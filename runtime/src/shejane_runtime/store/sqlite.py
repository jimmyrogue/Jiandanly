"""SQLite-backed local store. Async via aiosqlite.

Schema mirrors the Node runtime's tables conceptually but is intentionally
slimmer: anything LangGraph owns (message history, graph checkpoints) lives
in `agent.db` via AsyncSqliteSaver, not here.

Tables in this file:
- `local_workspaces`  — authorized filesystem roots
- `local_commands`    — immutable client commands and their accepted run
- `local_run_jobs`    — durable pending/leased execution attempts
- `local_runs`        — run metadata (status, goal, parent, settings, metadata)
- `local_events`      — append-only event log (one row per emit)
- `local_permissions` — pending / resolved permission requests
- `local_questions`   — pending / answered user questions
- `local_artifacts`   — tool-produced artifacts (file content, snapshots)
- `local_run_inputs`  — immutable Runtime-owned bodies admitted for one run
- `local_steering`    — user instructions queued into an active run
- `local_plan_approvals` — pending / resolved plan-mode approvals
- `local_scheduled_runs` — local-only delayed run requests
- `model_connections` — non-secret model-service connection configuration
- `local_runtime_settings` — persisted defaults for future runs
- `local_mcp_catalog` — credential-free MCP tool metadata and refresh state
- `plugin_versions` — immutable content-addressed plugin package metadata
- `plugin_installations` — principal-scoped active version and enabled state
- `local_model_calls` — durable model-call reservations and usage receipts
- `local_assistant_drafts` — latest complete top-level assistant model round
- `local_agent_messages` — durable same-root Agent mailbox envelopes
- `local_child_coordination` — immutable child completion/dependency policy
- `local_collaboration_resource_claims` — one writer owner per root resource
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path, PurePosixPath
from typing import Any

import aiosqlite

from ..auth import LOCAL_OWNER_PRINCIPAL_ID
from ..model_credentials import delete_legacy_model_api_key
from ..permission_policy import require_allowed_permission_scope
from ..plugins.identity import plugin_action_catalog_hash

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_BLOB_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_RUN_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
MAX_PRINCIPAL_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024 * 1024
MAX_SETTLEMENT_ARTIFACT_REFS = 256
MAX_RUN_INPUT_BYTES = 200 * 1024 * 1024


def _principal_thread_id(principal_id: str, requested_id: str) -> str:
    """Scope client-chosen IDs for future remote principals without moving local IDs."""
    if principal_id == LOCAL_OWNER_PRINCIPAL_ID and not requested_id.startswith("pt_"):
        return requested_id
    principal_prefix = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:16]
    prefix = f"pt_{principal_prefix}_"
    if requested_id.startswith(prefix):
        return requested_id
    logical_hash = hashlib.sha256(requested_id.encode("utf-8")).hexdigest()
    return f"{prefix}{logical_hash}"


def _principal_item_id(principal_id: str, requested_id: str) -> str:
    """Keep local item IDs stable and isolate remote client-chosen physical keys."""
    if principal_id == LOCAL_OWNER_PRINCIPAL_ID and not requested_id.startswith("pi_"):
        return requested_id
    principal_prefix = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:16]
    prefix = f"pi_{principal_prefix}_"
    if requested_id.startswith(prefix):
        return requested_id
    logical_hash = hashlib.sha256(requested_id.encode("utf-8")).hexdigest()
    return f"{prefix}{logical_hash}"


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS local_workspaces (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    path TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    UNIQUE (principal_id, path)
);

CREATE TABLE IF NOT EXISTS model_connections (
    principal_id TEXT NOT NULL,
    id TEXT NOT NULL,
    preset_id TEXT NOT NULL,
    name TEXT NOT NULL,
    region TEXT NOT NULL CHECK (region IN ('cn', 'intl', 'custom', 'official')),
    adapter_id TEXT NOT NULL CHECK (
        adapter_id IN ('openai_chat', 'anthropic_messages', 'google_genai')
    ),
    base_url TEXT NOT NULL,
    requires_api_key INTEGER NOT NULL DEFAULT 1,
    credential_ref TEXT NOT NULL,
    models_json TEXT NOT NULL,
    catalog_status TEXT NOT NULL CHECK (catalog_status IN ('ready', 'stale', 'unavailable')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, id)
);

CREATE TABLE IF NOT EXISTS model_capability_bindings (
    principal_id TEXT NOT NULL,
    capability TEXT NOT NULL CHECK (capability IN ('image_generation', 'image_editing')),
    connection_id TEXT NOT NULL,
    connection_version INTEGER NOT NULL CHECK (connection_version >= 1),
    model_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, capability),
    FOREIGN KEY (principal_id, connection_id)
        REFERENCES model_connections(principal_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS local_runtime_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    settings_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_mcp_catalog (
    server_name TEXT PRIMARY KEY,
    config_fingerprint TEXT NOT NULL,
    tools_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('ready', 'error', 'stale')),
    error_type TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    last_success_at TEXT
);

CREATE TABLE IF NOT EXISTS local_runs (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL DEFAULT 'local:owner',
    run_kind TEXT NOT NULL DEFAULT 'turn' CHECK (run_kind IN ('turn', 'fork', 'child')),
    root_run_id TEXT,
    agent_definition_id TEXT NOT NULL DEFAULT 'shejane.default',
    agent_definition_version TEXT NOT NULL DEFAULT '1',
    collaboration_depth INTEGER NOT NULL DEFAULT 0 CHECK (collaboration_depth >= 0),
    collaboration_policy_json TEXT NOT NULL DEFAULT '{}',
    spawn_operation_id TEXT,
    graph_thread_id TEXT NOT NULL,
    graph_checkpoint_id TEXT,
    graph_definition_id TEXT,
    graph_input_kind TEXT NOT NULL DEFAULT 'new',
    thread_id TEXT,
    assistant_item_id TEXT,
    user_input TEXT,
    goal TEXT NOT NULL,
    workspace_path TEXT,
    status TEXT NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]',
    parent_run_id TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    -- Resolved tier (fast|deep) once known, else the requested mode. Persisted
    -- so a HITL resume AFTER a runtime restart keeps the user's chosen tier
    -- instead of silently downgrading to fast. Added late, hence the additive
    -- migration in _ensure_columns() for DBs created before this column.
    mode TEXT NOT NULL DEFAULT 'fast',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS local_threads (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_threads_owner_updated
    ON local_threads(principal_id, updated_at DESC, id);

CREATE TABLE IF NOT EXISTS local_thread_items (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT,
    client_id TEXT,
    item_type TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    position INTEGER NOT NULL,
    event_high_watermark INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    superseded_at TEXT,
    superseded_by_run_id TEXT,
    FOREIGN KEY (thread_id) REFERENCES local_threads(id),
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_thread_items_client
    ON local_thread_items(thread_id, client_id) WHERE client_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_thread_items_run_type
    ON local_thread_items(run_id, item_type) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_local_thread_items_order
    ON local_thread_items(thread_id, created_at, id);

CREATE TABLE IF NOT EXISTS local_thread_changes (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    thread_version INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (thread_id) REFERENCES local_threads(id),
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_thread_changes_owner_cursor
    ON local_thread_changes(principal_id, cursor);

CREATE TABLE IF NOT EXISTS local_commands (
    principal_id TEXT NOT NULL,
    id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    response_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, id),
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);

CREATE TABLE IF NOT EXISTS plugin_versions (
    plugin_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    execution_kind TEXT NOT NULL CHECK (execution_kind IN ('wasi', 'managed_worker', 'builtin')),
    signature_status TEXT NOT NULL CHECK (signature_status IN ('unsigned', 'verified')),
    signer_key_id TEXT,
    compatibility TEXT NOT NULL CHECK (compatibility IN ('compatible', 'incompatible')),
    source TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('installed', 'retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT,
    PRIMARY KEY (plugin_id, digest),
    UNIQUE (plugin_id, version)
);

CREATE TABLE IF NOT EXISTS plugin_installations (
    principal_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    active_digest TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    source TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    model_binding_json TEXT,
    model_binding_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT,
    PRIMARY KEY (principal_id, plugin_id),
    FOREIGN KEY (active_digest) REFERENCES plugin_versions(digest)
);

CREATE TABLE IF NOT EXISTS plugin_setup_flows (
    principal_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN (
            'idle',
            'screen_requested',
            'screen_settings_opened',
            'accessibility_requested',
            'accessibility_settings_opened'
        )
    ),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, plugin_id)
);

CREATE TABLE IF NOT EXISTS run_plugin_bindings (
    run_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    selection_source TEXT NOT NULL
        CHECK (selection_source IN ('explicit', 'command', 'enabled')),
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    command_id TEXT,
    action_catalog_hash TEXT NOT NULL,
    model_binding_json TEXT,
    PRIMARY KEY (run_id, plugin_id),
    FOREIGN KEY (run_id) REFERENCES local_runs(id),
    FOREIGN KEY (plugin_id, digest) REFERENCES plugin_versions(plugin_id, digest)
);
CREATE INDEX IF NOT EXISTS idx_run_plugin_bindings_digest
    ON run_plugin_bindings(digest);

CREATE TABLE IF NOT EXISTS local_run_jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('start', 'resume')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'leased', 'completed', 'dead', 'canceled')),
    input_json TEXT NOT NULL,
    resume_json TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    cancel_requested_at TEXT,
    quarantined_at TEXT,
    quarantine_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_run_jobs_one_active
    ON local_run_jobs(run_id) WHERE status IN ('pending', 'leased');
CREATE INDEX IF NOT EXISTS idx_local_run_jobs_pending
    ON local_run_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS local_model_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    execution_attempt_id TEXT NOT NULL,
    call_index INTEGER NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'agent',
    parent_tool_operation_id TEXT,
    logical_call_id TEXT NOT NULL,
    retry_attempt INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (
        status IN (
            'reserved', 'streaming', 'completed', 'completed_unmetered',
            'failed', 'outcome_unknown'
        )
    ),
    output_started INTEGER NOT NULL DEFAULT 0,
    provider_request_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    first_output_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id),
    FOREIGN KEY (parent_tool_operation_id) REFERENCES local_tool_receipts(operation_id),
    UNIQUE (run_id, execution_attempt_id, call_index)
);

CREATE TABLE IF NOT EXISTS local_tool_receipts (
    operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    execution_attempt_id TEXT NOT NULL,
    execution_namespace TEXT NOT NULL,
    parent_operation_id TEXT,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL, -- prepared | running | paused | completed | failed | outcome_unknown | rejected | canceled
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    result_hash TEXT,
    error_type TEXT,
    review_decision TEXT,
    review_source TEXT,
    review_reason TEXT,
    review_model TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES local_runs(id),
    FOREIGN KEY (parent_operation_id) REFERENCES local_tool_receipts(operation_id),
    UNIQUE (run_id, execution_namespace, tool_call_id)
);
CREATE INDEX IF NOT EXISTS idx_local_tool_receipts_run_status
    ON local_tool_receipts(run_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_local_model_calls_run
    ON local_model_calls(run_id, call_index);

CREATE TABLE IF NOT EXISTS local_agent_messages (
    id TEXT PRIMARY KEY,
    root_run_id TEXT NOT NULL,
    sender_run_id TEXT NOT NULL,
    recipient_run_id TEXT NOT NULL,
    sender_operation_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('request', 'question', 'update', 'result', 'cancel')),
    text TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    correlation_id TEXT NOT NULL,
    in_reply_to TEXT,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    hop_count INTEGER NOT NULL CHECK (hop_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('queued', 'delivered', 'acknowledged', 'expired')),
    ttl_seconds INTEGER NOT NULL CHECK (ttl_seconds >= 60),
    deadline_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    acknowledged_at TEXT,
    FOREIGN KEY (root_run_id) REFERENCES local_runs(id),
    FOREIGN KEY (sender_run_id) REFERENCES local_runs(id),
    FOREIGN KEY (recipient_run_id) REFERENCES local_runs(id),
    FOREIGN KEY (sender_operation_id) REFERENCES local_tool_receipts(operation_id),
    FOREIGN KEY (in_reply_to) REFERENCES local_agent_messages(id)
);
CREATE INDEX IF NOT EXISTS idx_local_agent_messages_inbox
    ON local_agent_messages(recipient_run_id, status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_local_agent_messages_outbox
    ON local_agent_messages(sender_run_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_local_agent_messages_correlation
    ON local_agent_messages(correlation_id, sequence, id);

CREATE TABLE IF NOT EXISTS local_child_coordination (
    child_run_id TEXT PRIMARY KEY,
    root_run_id TEXT NOT NULL,
    parent_run_id TEXT NOT NULL,
    completion_mode TEXT NOT NULL
        CHECK (completion_mode IN ('required', 'best_effort', 'quorum')),
    quorum_group TEXT,
    quorum_required INTEGER CHECK (quorum_required IS NULL OR quorum_required >= 1),
    created_at TEXT NOT NULL,
    CHECK (
        (completion_mode = 'quorum' AND quorum_group IS NOT NULL AND quorum_required IS NOT NULL)
        OR
        (completion_mode != 'quorum' AND quorum_group IS NULL AND quorum_required IS NULL)
    ),
    FOREIGN KEY (child_run_id) REFERENCES local_runs(id),
    FOREIGN KEY (root_run_id) REFERENCES local_runs(id),
    FOREIGN KEY (parent_run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_child_coordination_parent
    ON local_child_coordination(parent_run_id, completion_mode, quorum_group, child_run_id);

CREATE TABLE IF NOT EXISTS local_child_dependencies (
    child_run_id TEXT NOT NULL,
    dependency_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (child_run_id, dependency_run_id),
    CHECK (child_run_id != dependency_run_id),
    FOREIGN KEY (child_run_id) REFERENCES local_runs(id),
    FOREIGN KEY (dependency_run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_child_dependencies_dependency
    ON local_child_dependencies(dependency_run_id, child_run_id);

CREATE TABLE IF NOT EXISTS local_collaboration_resource_claims (
    root_run_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    owner_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (root_run_id, resource_key),
    FOREIGN KEY (root_run_id) REFERENCES local_runs(id),
    FOREIGN KEY (owner_run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_collaboration_resource_owner
    ON local_collaboration_resource_claims(owner_run_id, resource_key);

-- Sandbox launchers this Runtime spawned but cannot clean up if it is killed
-- outright. The in-process paths (timeout, cancellation, failure) reap their
-- own trees; a SIGKILLed Runtime runs no code at all, so the row left here is
-- the only evidence the next Runtime has that a sandbox is still running.
-- pid alone would be unsafe to act on, so each row also carries the identity
-- needed to prove the pid was not recycled before anything is signalled.
CREATE TABLE IF NOT EXISTS local_sandbox_processes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    execution_attempt_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_started_at TEXT NOT NULL,
    settings_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'orphaned', 'reaped', 'gone', 'stale')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_sandbox_processes_attempt
    ON local_sandbox_processes(run_id, execution_attempt_id, status);
CREATE INDEX IF NOT EXISTS idx_local_sandbox_processes_status
    ON local_sandbox_processes(status, created_at);

CREATE TABLE IF NOT EXISTS local_assistant_drafts (
    run_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    message_key TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);

CREATE TABLE IF NOT EXISTS local_scheduled_runs (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    workspace_path TEXT,
    model TEXT NOT NULL DEFAULT 'auto',
    history_json TEXT NOT NULL DEFAULT '[]',
    settings_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    run_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    run_id TEXT,
    result_text TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    notified_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_scheduled_runs_due
    ON local_scheduled_runs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_local_scheduled_runs_notify
    ON local_scheduled_runs(status, notified_at, updated_at);

CREATE TABLE IF NOT EXISTS local_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_events_run_seq ON local_events(run_id, seq);

CREATE TABLE IF NOT EXISTS local_permissions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_call_id TEXT,
    wait_cycle_id TEXT,
    interrupt_id TEXT,
    action_index INTEGER NOT NULL DEFAULT 0,
    operation_id TEXT,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL DEFAULT '',
    arguments_hash TEXT,
    arguments_json TEXT NOT NULL,
    risk TEXT,
    decision_json TEXT,
    status TEXT NOT NULL,                -- pending | approved | denied | canceled
    scope TEXT NOT NULL DEFAULT 'once',  -- once | run
    grant_max_uses INTEGER NOT NULL DEFAULT 0,
    grant_use_count INTEGER NOT NULL DEFAULT 0,
    grant_expires_at TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE TABLE IF NOT EXISTS local_permission_grant_uses (
    permission_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, operation_id),
    FOREIGN KEY (permission_id) REFERENCES local_permissions(id),
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);

CREATE TABLE IF NOT EXISTS local_wait_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- tool_review | question | plan | tool_reconciliation
    wait_cycle_id TEXT NOT NULL,
    interrupt_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,                -- pending | resolved
    payload_json TEXT NOT NULL,
    decision_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_wait_candidates_run_status
    ON local_wait_candidates(run_id, status, created_at);
CREATE TABLE IF NOT EXISTS local_questions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_call_id TEXT,
    wait_cycle_id TEXT,
    interrupt_id TEXT,
    questions_json TEXT NOT NULL,
    status TEXT NOT NULL,                -- pending | answered | canceled
    answers_json TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE TABLE IF NOT EXISTS local_artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    storage_kind TEXT NOT NULL DEFAULT 'inline_text',
    blob_key TEXT,
    sha256 TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);

CREATE TABLE IF NOT EXISTS local_run_inputs (
    run_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    virtual_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    blob_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, input_id),
    UNIQUE (run_id, virtual_path),
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_run_inputs_sha256
    ON local_run_inputs(sha256);

CREATE TABLE IF NOT EXISTS local_steering (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | injected
    created_at TEXT NOT NULL,
    injected_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_local_steering_run_status
    ON local_steering(run_id, status, created_at);

CREATE TABLE IF NOT EXISTS local_plan_approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    wait_cycle_id TEXT,
    interrupt_id TEXT,
    todos_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | modified | rejected | canceled
    instructions TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES local_runs(id),
    UNIQUE (run_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS idx_local_plan_approvals_run_status
    ON local_plan_approvals(run_id, status, created_at);

"""


def _lease_expiry_error(cleanup_report: dict[str, Any]) -> str:
    """Say what was actually established about the work this attempt left running.

    The run is quarantined either way -- model calls may already have had
    effects nothing can undo. What changes is whether the sandboxed commands
    were confirmed stopped, which is the part a reader can act on.
    """

    if cleanup_report.get("status") == "completed":
        return (
            "The execution lease expired. Every sandboxed command this attempt started has "
            "been stopped, but the run is quarantined and cannot be retried automatically."
        )
    return (
        "The execution lease expired before the Runtime could prove that external work "
        "stopped. This run is quarantined and cannot be retried automatically."
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


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


class RunInputSnapshotError(RuntimeError):
    """A selected local input could not become an immutable Runtime body."""


class RunInputQuotaError(RunInputSnapshotError):
    """A Run input exceeds the immutable input-store safety budget."""


class LeaseFenceError(RuntimeError):
    """An execution attempted to write after losing its job lease."""


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


class ArtifactQuotaError(RuntimeError):
    """An artifact write would exceed a local persistence safety budget."""

    code = "artifact_quota_exceeded"
    retryable = False


class ArtifactConflictError(RuntimeError):
    """An artifact id was replayed with different immutable content."""

    code = "artifact_conflict"
    retryable = False


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    job_id: str
    run_id: str
    lease_owner: str
    lease_generation: int


_CURRENT_EXECUTION_LEASE: ContextVar[ExecutionLease | None] = ContextVar(
    "shejane_execution_lease", default=None
)


_RUN_RESULT_EVENTS = {
    "completed": "run.completed",
    "failed": "run.failed",
    "canceled": "run.canceled",
    "waiting_permission": "run.waiting",
    "waiting_input": "run.waiting",
}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled"}
TRANSIENT_RUN_EVENT_TYPES = frozenset(
    {
        "llm.delta",
        "llm.round.closed",
        "llm.round.started",
        "llm.reasoning",
        "llm.usage",
        "llm.tool_call_chunk",
        "tool.progress",
    }
)

_PRESENTATION_EVENT_TYPES = (
    "assistant.round.committed",
    "tool.requested",
    "tool.completed",
    "tool.failed",
    "tool.canceled",
    "subagent.spawned",
    "subagent.started",
    "subagent.waiting",
    "subagent.completed",
    "subagent.failed",
    "subagent.canceled",
    "subagent.outcome_unknown",
    "permission.required",
    "permission.resolved",
    "question.asked",
    "question.answered",
    "plan.approval_required",
    "plan.resolved",
    "tool.reconciliation_required",
    "tool.reconciliation_resolved",
    "artifact.created",
    "run.completed",
    "run.failed",
    "run.canceled",
    "run.cleanup_required",
)

_SUBAGENT_EVENT_BY_RECEIPT_STATUS = {
    "prepared": ("subagent.spawned", "queued"),
    "running": ("subagent.started", "running"),
    "paused": ("subagent.waiting", "waiting"),
    "completed": ("subagent.completed", "completed"),
    "failed": ("subagent.failed", "failed"),
    "rejected": ("subagent.failed", "failed"),
    "canceled": ("subagent.canceled", "canceled"),
    "outcome_unknown": ("subagent.outcome_unknown", "unknown"),
}

MAX_DURABLE_CHILD_DEPTH = 1
MAX_DURABLE_CHILDREN_PER_RUN = 8
MAX_DURABLE_CHILD_DEPENDENCIES = MAX_DURABLE_CHILDREN_PER_RUN - 1
MAX_DURABLE_CHILD_RESOURCE_CLAIMS = 16
MAX_AGENT_MAILBOX_PENDING = 32
MAX_AGENT_MAILBOX_MESSAGES_PER_ROOT = 512
MAX_AGENT_MAILBOX_HOPS = 8
MAX_AGENT_MAILBOX_ARTIFACT_REFS = 16
MAX_AGENT_MAILBOX_TEXT_BYTES = 32 * 1024
MAX_AGENT_MAILBOX_DATA_BYTES = 16 * 1024
_CHILD_EVENT_BY_RUN_EVENT = {
    "run.started": ("child.started", "running"),
    "run.resumed": ("child.started", "running"),
    "run.waiting": ("child.waiting", None),
    "run.completed": ("child.completed", "completed"),
    "run.failed": ("child.failed", "failed"),
    "run.canceled": ("child.canceled", "canceled"),
    "run.cleanup_required": ("child.cleanup_required", "cleanup_required"),
}


def _encode_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _json_payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_workspace_resource_key(workspace_path: str | None, requested_path: str) -> str:
    if not workspace_path:
        raise RunAdmissionError(
            "child_resource_workspace_required",
            "resource ownership requires an authorized workspace",
        )
    raw = str(requested_path).strip()
    if not raw or "\x00" in raw:
        raise ValueError("collaboration resource path is invalid")
    workspace = Path(workspace_path).resolve(strict=False)
    candidate = Path(raw)
    if candidate.is_absolute():
        absolute = candidate.resolve(strict=False)
        resolved = (
            absolute
            if absolute.is_relative_to(workspace)
            else (workspace / raw.lstrip("/\\")).resolve(strict=False)
        )
    else:
        resolved = (workspace / candidate).resolve(strict=False)
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise RunAdmissionError(
            "child_resource_outside_workspace",
            "collaboration resource path must stay inside the authorized workspace",
        ) from exc
    key = PurePosixPath(relative.as_posix()).as_posix()
    if key in {"", "."}:
        raise ValueError("collaboration resource must identify a file inside the workspace")
    if len(key.encode("utf-8")) > 4096:
        raise ValueError("collaboration resource path is too long")
    return key


def _normalize_child_coordination(
    coordination: dict[str, Any] | None,
    *,
    workspace_path: str | None,
) -> dict[str, Any]:
    raw = dict(coordination or {})
    unknown = set(raw) - {
        "completion_mode",
        "depends_on",
        "resource_claims",
        "quorum_group",
        "quorum_required",
    }
    if unknown:
        raise ValueError(f"unknown child coordination fields: {', '.join(sorted(unknown))}")

    completion_mode = str(raw.get("completion_mode") or "required").strip()
    if completion_mode not in {"required", "best_effort", "quorum"}:
        raise ValueError("child completion_mode is invalid")

    depends_raw = raw.get("depends_on") or []
    if isinstance(depends_raw, (str, bytes)) or not isinstance(depends_raw, Sequence):
        raise ValueError("child depends_on must be a list")
    depends_on = [str(run_id).strip() for run_id in depends_raw]
    if (
        len(depends_on) > MAX_DURABLE_CHILD_DEPENDENCIES
        or len(depends_on) != len(set(depends_on))
        or any(not run_id or len(run_id) > 128 for run_id in depends_on)
    ):
        raise ValueError("child depends_on is invalid")

    claims_raw = raw.get("resource_claims") or []
    if isinstance(claims_raw, (str, bytes)) or not isinstance(claims_raw, Sequence):
        raise ValueError("child resource_claims must be a list")
    resource_claims = [
        _normalize_workspace_resource_key(workspace_path, str(path)) for path in claims_raw
    ]
    if len(resource_claims) > MAX_DURABLE_CHILD_RESOURCE_CLAIMS or len(resource_claims) != len(
        set(resource_claims)
    ):
        raise ValueError("child resource_claims is invalid")

    quorum_group_raw = raw.get("quorum_group")
    quorum_required_raw = raw.get("quorum_required")
    if completion_mode == "quorum":
        quorum_group = str(quorum_group_raw or "").strip()
        if not quorum_group or len(quorum_group) > 128:
            raise ValueError("quorum children require a quorum_group")
        if isinstance(quorum_required_raw, bool) or not isinstance(quorum_required_raw, int):
            raise ValueError("quorum children require quorum_required")
        if not 1 <= quorum_required_raw <= MAX_DURABLE_CHILDREN_PER_RUN:
            raise ValueError("child quorum_required is invalid")
        quorum_required: int | None = quorum_required_raw
    else:
        if quorum_group_raw is not None or quorum_required_raw is not None:
            raise ValueError("quorum fields are only valid for quorum children")
        quorum_group = None
        quorum_required = None

    return {
        "completion_mode": completion_mode,
        "depends_on": depends_on,
        "resource_claims": resource_claims,
        "quorum_group": quorum_group,
        "quorum_required": quorum_required,
    }


def _agent_message_projection(row: dict[str, Any]) -> dict[str, Any]:
    projected = dict(row)
    projected["data"] = _json_payload(projected.pop("data_json", None))
    try:
        artifact_refs = json.loads(str(projected.pop("artifact_refs_json", "[]")))
    except (json.JSONDecodeError, TypeError):
        artifact_refs = []
    projected["artifact_refs"] = artifact_refs if isinstance(artifact_refs, list) else []
    projected["sequence"] = int(projected["sequence"])
    projected["hop_count"] = int(projected["hop_count"])
    projected["ttl_seconds"] = int(projected["ttl_seconds"])
    return projected


def _normalize_agent_message_content(
    *,
    kind: str,
    text: str,
    data: dict[str, Any],
    artifact_refs: Sequence[str],
    ttl_seconds: int,
) -> tuple[str, str, str, str, int]:
    normalized_kind = str(kind).strip().lower()
    if normalized_kind not in {"request", "question", "update", "result", "cancel"}:
        raise ValueError("Agent message kind is invalid")
    normalized_text = str(text).strip()
    if len(normalized_text.encode("utf-8")) > MAX_AGENT_MAILBOX_TEXT_BYTES:
        raise ValueError("Agent message text is too large")
    if not isinstance(data, dict):
        raise ValueError("Agent message data must be an object")
    try:
        data_json = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent message data must be JSON-compatible") from exc
    if len(data_json.encode("utf-8")) > MAX_AGENT_MAILBOX_DATA_BYTES:
        raise ValueError("Agent message data is too large")
    if isinstance(artifact_refs, (str, bytes)):
        raise ValueError("Agent message artifact_refs must be a list")
    normalized_refs = list(dict.fromkeys(str(ref).strip() for ref in artifact_refs))
    if len(normalized_refs) > MAX_AGENT_MAILBOX_ARTIFACT_REFS or any(
        not ref or len(ref) > 512 for ref in normalized_refs
    ):
        raise ValueError("Agent message artifact_refs are invalid")
    if not normalized_text and not data and not normalized_refs:
        raise ValueError("Agent message content is empty")
    if isinstance(ttl_seconds, bool) or not 60 <= int(ttl_seconds) <= 24 * 60 * 60:
        raise ValueError("Agent message ttl_seconds must be between 60 and 86400")
    return (
        normalized_kind,
        normalized_text,
        data_json,
        _encode_payload(normalized_refs),
        int(ttl_seconds),
    )


def _normalize_child_agent_definition(value: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "id",
        "version",
        "name",
        "description",
        "system_prompt",
        "allowed_tools",
    }
    if set(value) != allowed_keys:
        raise ValueError("child Agent definition has invalid fields")
    normalized: dict[str, Any] = {}
    for key, maximum in (
        ("id", 128),
        ("version", 128),
        ("name", 64),
        ("description", 4096),
        ("system_prompt", 32 * 1024),
    ):
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.strip() or len(raw) > maximum:
            raise ValueError(f"child Agent definition {key} is invalid")
        normalized[key] = raw.strip()
    raw_tools = value.get("allowed_tools")
    if (
        not isinstance(raw_tools, list)
        or len(raw_tools) > 128
        or any(not isinstance(name, str) or not name or len(name) > 128 for name in raw_tools)
    ):
        raise ValueError("child Agent definition allowed_tools is invalid")
    normalized["allowed_tools"] = sorted(set(raw_tools))
    if len(_encode_payload(normalized).encode("utf-8")) > 64 * 1024:
        raise ValueError("child Agent definition is too large")
    return normalized


def _decode_mcp_catalog_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    try:
        tools = json.loads(str(record.pop("tools_json", "[]")))
    except (json.JSONDecodeError, TypeError):
        tools = []
    record["tools"] = tools if isinstance(tools, list) else []
    record["version"] = int(record["version"])
    return record


def _decode_plan_approval_record(record: dict[str, Any]) -> dict[str, Any]:
    try:
        todos = json.loads(str(record.get("todos_json") or "[]"))
    except json.JSONDecodeError:
        todos = []
    return {
        **record,
        "todos": todos if isinstance(todos, list) else [],
    }


async def _configure_connection(conn: aiosqlite.Connection) -> None:
    """Apply invariants that SQLite scopes to each individual connection."""
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")


async def _finish_task_despite_cancellation[T](task: asyncio.Task[T]) -> T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return await task


class LocalStore:
    """Thin async wrapper over aiosqlite. Connection-per-store."""

    def __init__(self, conn: aiosqlite.Connection, db_path: Path) -> None:
        self._conn = conn
        self._db_path = db_path

    @classmethod
    async def open(cls, db_path: Path) -> LocalStore:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path), isolation_level=None)
        try:
            await _configure_connection(conn)
            await conn.executescript(SCHEMA)
            await cls._ensure_model_connection_constraints(conn)
            await cls._ensure_plugin_execution_kinds(conn)
            await cls._delete_legacy_model_provider_credentials(conn)
            await conn.execute("BEGIN IMMEDIATE")
            await cls._ensure_columns(conn)
            await conn.commit()
            store = cls(conn, db_path)
            await store.gc_orphan_bodies()
            return store
        except BaseException:
            if conn.in_transaction:
                await conn.rollback()
            await conn.close()
            raise

    @staticmethod
    async def _ensure_model_connection_constraints(conn: aiosqlite.Connection) -> None:
        row = await (
            await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'model_connections'"
            )
        ).fetchone()
        if row is None or ("google_genai" in str(row[0]) and "official" in str(row[0])):
            return
        await conn.execute("PRAGMA foreign_keys = OFF")
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "CREATE TABLE model_connections_next ("
                "principal_id TEXT NOT NULL, id TEXT NOT NULL, preset_id TEXT NOT NULL, "
                "name TEXT NOT NULL, region TEXT NOT NULL "
                "CHECK (region IN ('cn', 'intl', 'custom', 'official')), "
                "adapter_id TEXT NOT NULL "
                "CHECK (adapter_id IN ('openai_chat', 'anthropic_messages', 'google_genai')), "
                "base_url TEXT NOT NULL, requires_api_key INTEGER NOT NULL DEFAULT 1, "
                "credential_ref TEXT NOT NULL, models_json TEXT NOT NULL, "
                "catalog_status TEXT NOT NULL "
                "CHECK (catalog_status IN ('ready', 'stale', 'unavailable')), "
                "version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, PRIMARY KEY (principal_id, id))"
            )
            await conn.execute("INSERT INTO model_connections_next SELECT * FROM model_connections")
            await conn.execute("DROP TABLE model_connections")
            await conn.execute("ALTER TABLE model_connections_next RENAME TO model_connections")
            await conn.commit()
        except BaseException:
            if conn.in_transaction:
                await conn.rollback()
            raise
        finally:
            await conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    async def _delete_legacy_model_provider_credentials(
        conn: aiosqlite.Connection,
    ) -> None:
        table = await (
            await conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'local_model_providers'"
            )
        ).fetchone()
        if table is None:
            return
        rows = await (await conn.execute("SELECT * FROM local_model_providers")).fetchall()
        for row in rows:
            record = dict(row)
            await delete_legacy_model_api_key(
                str(record.get("principal_id") or LOCAL_OWNER_PRINCIPAL_ID),
                str(record["id"]),
                str(record["credential_ref"]) if record.get("credential_ref") else None,
            )

    @staticmethod
    async def _ensure_columns(conn: aiosqlite.Connection) -> None:
        """Additive migrations for DBs created before a column existed.
        `CREATE TABLE IF NOT EXISTS` never alters an existing table, so a new
        column has to be added explicitly. SQLite ADD COLUMN is cheap + safe."""
        await LocalStore._ensure_principal_scoped_workspaces(conn)
        await conn.execute("DROP TABLE IF EXISTS local_model_providers")
        cursor = await conn.execute("PRAGMA table_info(local_runs)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "mode" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'fast'"
            )
        if "metadata_json" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "principal_id" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN principal_id TEXT NOT NULL DEFAULT 'local:owner'"
            )
        if "run_kind" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'turn' "
                "CHECK (run_kind IN ('turn', 'fork', 'child'))"
            )
        if "root_run_id" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN root_run_id TEXT")
            await conn.execute("UPDATE local_runs SET root_run_id = id WHERE root_run_id IS NULL")
        if "agent_definition_id" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN agent_definition_id TEXT NOT NULL "
                "DEFAULT 'shejane.default'"
            )
        if "agent_definition_version" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN agent_definition_version TEXT NOT NULL "
                "DEFAULT '1'"
            )
        if "collaboration_depth" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN collaboration_depth INTEGER NOT NULL "
                "DEFAULT 0 CHECK (collaboration_depth >= 0)"
            )
        if "collaboration_policy_json" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN collaboration_policy_json TEXT NOT NULL "
                "DEFAULT '{}'"
            )
        if "spawn_operation_id" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN spawn_operation_id TEXT")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_runs_parent_kind "
            "ON local_runs(parent_run_id, run_kind, created_at, id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_runs_root "
            "ON local_runs(root_run_id, created_at, id)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_runs_spawn_operation "
            "ON local_runs(spawn_operation_id) WHERE spawn_operation_id IS NOT NULL"
        )
        await conn.execute(
            "INSERT OR IGNORE INTO local_child_coordination "
            "(child_run_id, root_run_id, parent_run_id, completion_mode, quorum_group, "
            "quorum_required, created_at) "
            "SELECT id, COALESCE(root_run_id, parent_run_id), parent_run_id, 'required', "
            "NULL, NULL, created_at FROM local_runs "
            "WHERE run_kind = 'child' AND parent_run_id IS NOT NULL"
        )
        if "graph_thread_id" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN graph_thread_id TEXT")
            await conn.execute(
                "UPDATE local_runs SET graph_thread_id = id WHERE graph_thread_id IS NULL"
            )
        if "graph_checkpoint_id" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN graph_checkpoint_id TEXT")
        if "graph_definition_id" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN graph_definition_id TEXT")
        if "graph_input_kind" not in columns:
            await conn.execute(
                "ALTER TABLE local_runs ADD COLUMN graph_input_kind TEXT NOT NULL DEFAULT 'new'"
            )
        await conn.execute(
            "UPDATE local_runs SET run_kind = 'fork' "
            "WHERE graph_input_kind = 'fork' AND run_kind = 'turn'"
        )
        if "thread_id" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN thread_id TEXT")
        if "assistant_item_id" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN assistant_item_id TEXT")
        if "user_input" not in columns:
            await conn.execute("ALTER TABLE local_runs ADD COLUMN user_input TEXT")
            await conn.execute("UPDATE local_runs SET user_input = goal WHERE user_input IS NULL")
        cursor = await conn.execute("PRAGMA table_info(local_threads)")
        thread_columns = {row[1] for row in await cursor.fetchall()}
        if "metadata_json" not in thread_columns:
            await conn.execute(
                "ALTER TABLE local_threads ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "deleted_at" not in thread_columns:
            await conn.execute("ALTER TABLE local_threads ADD COLUMN deleted_at TEXT")
        cursor = await conn.execute("PRAGMA table_info(local_thread_items)")
        thread_item_columns = {row[1] for row in await cursor.fetchall()}
        if "position" not in thread_item_columns:
            await conn.execute(
                "ALTER TABLE local_thread_items ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
        if "superseded_at" not in thread_item_columns:
            await conn.execute("ALTER TABLE local_thread_items ADD COLUMN superseded_at TEXT")
        if "superseded_by_run_id" not in thread_item_columns:
            await conn.execute(
                "ALTER TABLE local_thread_items ADD COLUMN superseded_by_run_id TEXT"
            )
        if "event_high_watermark" not in thread_item_columns:
            await conn.execute(
                "ALTER TABLE local_thread_items "
                "ADD COLUMN event_high_watermark INTEGER NOT NULL DEFAULT 0"
            )
            await conn.execute(
                "UPDATE local_thread_items SET event_high_watermark = COALESCE(("
                "SELECT MAX(e.seq) FROM local_events e "
                "WHERE e.run_id = local_thread_items.run_id "
                "AND e.event_type IN ('run.waiting', 'run.completed', 'run.failed', "
                "'run.canceled', 'run.cleanup_required')"
                "), 0) WHERE item_type = 'assistant_message' AND run_id IS NOT NULL"
            )
        await conn.execute("DROP INDEX IF EXISTS idx_local_thread_items_order")
        await conn.execute(
            "CREATE INDEX idx_local_thread_items_order "
            "ON local_thread_items(thread_id, position, id)"
        )
        cursor = await conn.execute("PRAGMA table_info(local_scheduled_runs)")
        schedule_columns = {row[1] for row in await cursor.fetchall()}
        if "principal_id" not in schedule_columns:
            await conn.execute(
                "ALTER TABLE local_scheduled_runs ADD COLUMN principal_id "
                "TEXT NOT NULL DEFAULT 'local:owner'"
            )
        await LocalStore._ensure_principal_scoped_commands(conn)
        await LocalStore._ensure_generic_commands(conn)
        cursor = await conn.execute("PRAGMA table_info(plugin_installations)")
        plugin_installation_columns = {row[1] for row in await cursor.fetchall()}
        if "retired_at" not in plugin_installation_columns:
            await conn.execute("ALTER TABLE plugin_installations ADD COLUMN retired_at TEXT")
        if "model_binding_json" not in plugin_installation_columns:
            await conn.execute(
                "ALTER TABLE plugin_installations ADD COLUMN model_binding_json TEXT"
            )
        if "model_binding_revision" not in plugin_installation_columns:
            await conn.execute(
                "ALTER TABLE plugin_installations ADD COLUMN model_binding_revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
        cursor = await conn.execute("PRAGMA table_info(run_plugin_bindings)")
        run_plugin_binding_columns = {row[1] for row in await cursor.fetchall()}
        if "model_binding_json" not in run_plugin_binding_columns:
            await conn.execute("ALTER TABLE run_plugin_bindings ADD COLUMN model_binding_json TEXT")
        cursor = await conn.execute("PRAGMA table_info(plugin_versions)")
        plugin_version_columns = {row[1] for row in await cursor.fetchall()}
        if "signer_key_id" not in plugin_version_columns:
            await conn.execute("ALTER TABLE plugin_versions ADD COLUMN signer_key_id TEXT")
        await LocalStore._ensure_run_job_principals(conn)
        cursor = await conn.execute("PRAGMA table_info(local_run_jobs)")
        job_columns = {row[1] for row in await cursor.fetchall()}
        if "quarantined_at" not in job_columns:
            await conn.execute("ALTER TABLE local_run_jobs ADD COLUMN quarantined_at TEXT")
        if "quarantine_reason" not in job_columns:
            await conn.execute("ALTER TABLE local_run_jobs ADD COLUMN quarantine_reason TEXT")
        await LocalStore._ensure_permission_identity_columns(conn)
        await LocalStore._ensure_wait_identity_columns(conn)
        await LocalStore._ensure_tool_receipt_namespace(conn)
        await LocalStore._ensure_tool_receipt_version_column(conn)
        await LocalStore._ensure_tool_receipt_review_columns(conn)
        await LocalStore._ensure_tool_receipt_parent_column(conn)
        await LocalStore._ensure_model_call_purpose_column(conn)
        await LocalStore._ensure_model_call_parent_operation_column(conn)
        await LocalStore._ensure_model_call_retry_columns(conn)
        await LocalStore._ensure_wait_candidates(conn)
        await LocalStore._ensure_artifact_storage_columns(conn)
        transient_placeholders = ",".join("?" for _ in TRANSIENT_RUN_EVENT_TYPES)
        await conn.execute(
            f"DELETE FROM local_events WHERE event_type IN ({transient_placeholders})",
            tuple(sorted(TRANSIENT_RUN_EVENT_TYPES)),
        )
        # Before receipt-backed lifecycle events existed, `subagent.spawned`
        # was a best-effort projection of a partial model stream chunk. Those
        # rows have no stable operation identity and must not survive a reopen.
        # Receipt-backed spawn events carry `operation_id` and are authoritative.
        await conn.execute(
            "DELETE FROM local_events WHERE event_type = 'subagent.spawned' "
            "AND (json_valid(payload_json) = 0 "
            "OR COALESCE(json_extract(payload_json, '$.operation_id'), '') = '')"
        )
        # Lark integration was removed; delete its local-only cache and todo data.
        for table in (
            "local_todo_items",
            "local_lark_messages",
            "local_lark_sources",
            "local_lark_connections",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {table}")
        await LocalStore._ensure_event_sequence_index(conn)

    @staticmethod
    async def _ensure_artifact_storage_columns(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_artifacts)")
        columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in (
            ("storage_kind", "TEXT NOT NULL DEFAULT 'inline_text'"),
            ("blob_key", "TEXT"),
            ("sha256", "TEXT"),
        ):
            if column not in columns:
                await conn.execute(f"ALTER TABLE local_artifacts ADD COLUMN {column} {definition}")

    @staticmethod
    async def _ensure_plugin_execution_kinds(conn: aiosqlite.Connection) -> None:
        schema = await (
            await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'plugin_versions'"
            )
        ).fetchone()
        if schema is not None and "'builtin'" in str(schema[0]):
            return
        columns = {
            row[1]
            for row in await (await conn.execute("PRAGMA table_info(plugin_versions)")).fetchall()
        }
        signer_key = "signer_key_id" if "signer_key_id" in columns else "NULL"
        await conn.execute("PRAGMA foreign_keys = OFF")
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "CREATE TABLE plugin_versions_v2 ("
                "plugin_id TEXT NOT NULL, version TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, "
                "manifest_json TEXT NOT NULL, execution_kind TEXT NOT NULL "
                "CHECK (execution_kind IN ('wasi', 'managed_worker', 'builtin')), "
                "signature_status TEXT NOT NULL CHECK (signature_status IN ('unsigned', 'verified')), "
                "signer_key_id TEXT, compatibility TEXT NOT NULL "
                "CHECK (compatibility IN ('compatible', 'incompatible')), "
                "source TEXT NOT NULL, state TEXT NOT NULL "
                "CHECK (state IN ('installed', 'retired')), created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, retired_at TEXT, PRIMARY KEY (plugin_id, digest), "
                "UNIQUE (plugin_id, version))"
            )
            await conn.execute(
                "INSERT INTO plugin_versions_v2 "
                "(plugin_id, version, digest, manifest_json, execution_kind, signature_status, "
                "signer_key_id, compatibility, source, state, created_at, updated_at, retired_at) "
                "SELECT plugin_id, version, digest, manifest_json, execution_kind, "
                f"signature_status, {signer_key}, compatibility, source, state, created_at, "
                "updated_at, retired_at FROM plugin_versions"
            )
            await conn.execute("DROP TABLE plugin_versions")
            await conn.execute("ALTER TABLE plugin_versions_v2 RENAME TO plugin_versions")
            await conn.commit()
        except BaseException:
            if conn.in_transaction:
                await conn.rollback()
            raise
        finally:
            await conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    async def _ensure_permission_identity_columns(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_permissions)")
        columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in (
            ("operation_id", "TEXT"),
            ("tool_version", "TEXT NOT NULL DEFAULT ''"),
            ("arguments_hash", "TEXT"),
            ("risk", "TEXT"),
            ("decision_json", "TEXT"),
            ("grant_max_uses", "INTEGER NOT NULL DEFAULT 0"),
            ("grant_use_count", "INTEGER NOT NULL DEFAULT 0"),
            ("grant_expires_at", "TEXT"),
            ("wait_cycle_id", "TEXT"),
            ("interrupt_id", "TEXT"),
            ("action_index", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                await conn.execute(
                    f"ALTER TABLE local_permissions ADD COLUMN {column} {definition}"
                )
        await conn.execute(
            "UPDATE local_permissions SET wait_cycle_id = COALESCE(wait_cycle_id, id), "
            "interrupt_id = COALESCE(interrupt_id, id)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_permissions_run_operation "
            "ON local_permissions(run_id, operation_id) "
            "WHERE operation_id IS NOT NULL"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_permissions_interrupt_action "
            "ON local_permissions(run_id, interrupt_id, action_index) "
            "WHERE interrupt_id IS NOT NULL"
        )

    @staticmethod
    async def _ensure_wait_identity_columns(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_wait_candidates)")
        wait_columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in (
            ("wait_cycle_id", "TEXT"),
            ("interrupt_id", "TEXT"),
            ("position", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in wait_columns:
                await conn.execute(
                    f"ALTER TABLE local_wait_candidates ADD COLUMN {column} {definition}"
                )
        await conn.execute(
            "UPDATE local_wait_candidates SET wait_cycle_id = COALESCE(wait_cycle_id, id), "
            "interrupt_id = COALESCE(interrupt_id, id)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_wait_candidates_interrupt_position "
            "ON local_wait_candidates(run_id, interrupt_id, position)"
        )
        cursor = await conn.execute("PRAGMA table_info(local_questions)")
        question_columns = {row[1] for row in await cursor.fetchall()}
        for column in ("wait_cycle_id", "interrupt_id"):
            if column not in question_columns:
                await conn.execute(f"ALTER TABLE local_questions ADD COLUMN {column} TEXT")
        await conn.execute(
            "UPDATE local_questions SET wait_cycle_id = COALESCE(wait_cycle_id, id), "
            "interrupt_id = COALESCE(interrupt_id, id)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_questions_interrupt "
            "ON local_questions(run_id, interrupt_id) WHERE interrupt_id IS NOT NULL"
        )
        cursor = await conn.execute("PRAGMA table_info(local_plan_approvals)")
        plan_columns = {row[1] for row in await cursor.fetchall()}
        for column in ("wait_cycle_id", "interrupt_id"):
            if column not in plan_columns:
                await conn.execute(f"ALTER TABLE local_plan_approvals ADD COLUMN {column} TEXT")
        await LocalStore._backfill_plan_approval_wait_identity(conn)

    @staticmethod
    async def _backfill_plan_approval_wait_identity(conn: aiosqlite.Connection) -> None:
        plans = await (
            await conn.execute(
                "SELECT id, run_id, tool_call_id FROM local_plan_approvals "
                "WHERE wait_cycle_id IS NULL OR interrupt_id IS NULL"
            )
        ).fetchall()
        for plan in plans:
            approval_events = await (
                await conn.execute(
                    "SELECT seq, payload_json FROM local_events WHERE run_id = ? "
                    "AND event_type = 'plan.approval_required' ORDER BY seq",
                    (plan[1],),
                )
            ).fetchall()
            approval_seq = next(
                (
                    int(event[0])
                    for event in approval_events
                    if _json_payload(event[1]).get("request_id") == plan[0]
                ),
                None,
            )
            if approval_seq is None:
                continue
            waiting_events = await (
                await conn.execute(
                    "SELECT payload_json FROM local_events WHERE run_id = ? AND seq > ? "
                    "AND event_type = 'run.waiting' ORDER BY seq",
                    (plan[1], approval_seq),
                )
            ).fetchall()
            identity: tuple[str, str] | None = None
            for event in waiting_events:
                payload = _json_payload(event[0])
                wait_cycle_id = str(payload.get("wait_cycle_id") or "")
                interrupts = payload.get("interrupts")
                if not wait_cycle_id or not isinstance(interrupts, list):
                    continue
                candidates = [
                    interrupt
                    for interrupt in interrupts
                    if isinstance(interrupt, dict)
                    and isinstance(interrupt.get("value"), dict)
                    and interrupt["value"].get("kind") == "plan_approval"
                ]
                matching = [
                    interrupt
                    for interrupt in candidates
                    if interrupt["value"].get("tool_call_id") == plan[2]
                    or interrupt.get("id") == plan[2]
                ]
                candidate = matching[0] if len(matching) == 1 else None
                interrupt_id = str(candidate.get("id") or "") if candidate else ""
                if interrupt_id:
                    identity = (wait_cycle_id, interrupt_id)
                    break
            if identity is not None:
                await conn.execute(
                    "UPDATE local_plan_approvals SET wait_cycle_id = ?, interrupt_id = ? "
                    "WHERE id = ?",
                    (*identity, plan[0]),
                )

    @staticmethod
    async def _ensure_tool_receipt_version_column(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_tool_receipts)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "tool_version" not in columns:
            await conn.execute(
                "ALTER TABLE local_tool_receipts ADD COLUMN tool_version TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    async def _ensure_tool_receipt_review_columns(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_tool_receipts)")
        columns = {row[1] for row in await cursor.fetchall()}
        for column in (
            "review_decision",
            "review_source",
            "review_reason",
            "review_model",
            "reviewed_at",
        ):
            if column not in columns:
                await conn.execute(f"ALTER TABLE local_tool_receipts ADD COLUMN {column} TEXT")

    @staticmethod
    async def _ensure_tool_receipt_parent_column(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_tool_receipts)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "parent_operation_id" not in columns:
            await conn.execute(
                "ALTER TABLE local_tool_receipts ADD COLUMN parent_operation_id TEXT"
            )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_tool_receipts_parent "
            "ON local_tool_receipts(parent_operation_id, created_at)"
        )

    @staticmethod
    async def _ensure_model_call_purpose_column(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_model_calls)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "purpose" not in columns:
            await conn.execute(
                "ALTER TABLE local_model_calls ADD COLUMN purpose TEXT NOT NULL DEFAULT 'agent'"
            )

    @staticmethod
    async def _ensure_model_call_parent_operation_column(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_model_calls)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "parent_tool_operation_id" not in columns:
            await conn.execute(
                "ALTER TABLE local_model_calls ADD COLUMN parent_tool_operation_id TEXT"
            )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_model_calls_parent_tool "
            "ON local_model_calls(parent_tool_operation_id, call_index)"
        )

    @staticmethod
    async def _ensure_model_call_retry_columns(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_model_calls)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "logical_call_id" not in columns:
            await conn.execute("ALTER TABLE local_model_calls ADD COLUMN logical_call_id TEXT")
            await conn.execute(
                "UPDATE local_model_calls SET logical_call_id = id WHERE logical_call_id IS NULL"
            )
        if "retry_attempt" not in columns:
            await conn.execute(
                "ALTER TABLE local_model_calls ADD COLUMN retry_attempt INTEGER NOT NULL DEFAULT 0"
            )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_model_calls_logical "
            "ON local_model_calls(run_id, logical_call_id, retry_attempt)"
        )

    @staticmethod
    async def _ensure_tool_receipt_namespace(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_tool_receipts)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "execution_namespace" in columns:
            return
        tool_version_expr = "tool_version" if "tool_version" in columns else "''"
        await conn.execute("SAVEPOINT tool_receipt_namespace")
        try:
            await conn.execute(
                "CREATE TABLE local_tool_receipts_v2 ("
                "operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "execution_attempt_id TEXT NOT NULL, execution_namespace TEXT NOT NULL, "
                "tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, "
                "tool_version TEXT NOT NULL DEFAULT '', arguments_hash TEXT NOT NULL, "
                "arguments_json TEXT NOT NULL, risk TEXT NOT NULL, status TEXT NOT NULL, "
                "attempt_count INTEGER NOT NULL DEFAULT 0, result_json TEXT, "
                "result_hash TEXT, error_type TEXT, created_at TEXT NOT NULL, "
                "started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, "
                "FOREIGN KEY (run_id) REFERENCES local_runs(id), "
                "UNIQUE (run_id, execution_namespace, tool_call_id))"
            )
            await conn.execute(
                "INSERT INTO local_tool_receipts_v2 "
                "(operation_id, run_id, execution_attempt_id, execution_namespace, "
                "tool_call_id, tool_name, tool_version, arguments_hash, arguments_json, "
                "risk, status, attempt_count, result_json, result_hash, error_type, "
                "created_at, started_at, completed_at, updated_at) "
                "SELECT operation_id, run_id, execution_attempt_id, 'main', "
                f"tool_call_id, tool_name, {tool_version_expr}, arguments_hash, arguments_json, risk, status, "
                "attempt_count, result_json, result_hash, error_type, created_at, "
                "started_at, completed_at, updated_at FROM local_tool_receipts"
            )
            await conn.execute("DROP TABLE local_tool_receipts")
            await conn.execute("ALTER TABLE local_tool_receipts_v2 RENAME TO local_tool_receipts")
            await conn.execute(
                "CREATE INDEX idx_local_tool_receipts_run_status "
                "ON local_tool_receipts(run_id, status, created_at)"
            )
            await conn.execute("RELEASE tool_receipt_namespace")
        except BaseException:
            await conn.execute("ROLLBACK TO tool_receipt_namespace")
            await conn.execute("RELEASE tool_receipt_namespace")
            raise

    @staticmethod
    async def _ensure_wait_candidates(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT OR IGNORE INTO local_wait_candidates "
            "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
            "payload_json, decision_json, created_at, resolved_at) "
            "SELECT id, run_id, 'tool_review', COALESCE(wait_cycle_id, id), "
            "COALESCE(interrupt_id, id), action_index, "
            "CASE WHEN status = 'pending' THEN 'pending' ELSE 'resolved' END, "
            "arguments_json, decision_json, created_at, resolved_at FROM local_permissions"
        )
        await conn.execute(
            "INSERT OR IGNORE INTO local_wait_candidates "
            "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
            "payload_json, decision_json, created_at, resolved_at) "
            "SELECT id, run_id, 'question', COALESCE(wait_cycle_id, id), "
            "COALESCE(interrupt_id, id), 0, "
            "CASE WHEN status = 'pending' THEN 'pending' ELSE 'resolved' END, "
            "questions_json, answers_json, created_at, answered_at FROM local_questions"
        )
        await conn.execute(
            "INSERT OR IGNORE INTO local_wait_candidates "
            "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
            "payload_json, decision_json, created_at, resolved_at) "
            "SELECT id, run_id, 'plan', wait_cycle_id, interrupt_id, 0, 'pending', "
            "todos_json, NULL, created_at, NULL FROM local_plan_approvals "
            "WHERE status = 'pending' AND wait_cycle_id IS NOT NULL "
            "AND interrupt_id IS NOT NULL"
        )
        resolved_plans = await (
            await conn.execute(
                "SELECT id, run_id, wait_cycle_id, interrupt_id, todos_json, status, "
                "instructions, created_at, resolved_at FROM local_plan_approvals "
                "WHERE status IN ('approved', 'modified', 'rejected') "
                "AND wait_cycle_id IS NOT NULL AND interrupt_id IS NOT NULL"
            )
        ).fetchall()
        for plan in resolved_plans:
            decision = {
                "approved": "approve",
                "modified": "modify",
                "rejected": "reject",
            }[str(plan[5])]
            await conn.execute(
                "INSERT OR IGNORE INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (?, ?, 'plan', ?, ?, 0, 'resolved', ?, ?, ?, ?)",
                (
                    plan[0],
                    plan[1],
                    plan[2],
                    plan[3],
                    plan[4],
                    _encode_payload(
                        {
                            "approval_id": plan[0],
                            "decision": decision,
                            "instructions": plan[6],
                        }
                    ),
                    plan[7],
                    plan[8],
                ),
            )

    @staticmethod
    async def _ensure_principal_scoped_workspaces(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_workspaces)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "principal_id" in columns:
            return
        await conn.execute("SAVEPOINT principal_scoped_workspaces")
        try:
            await conn.execute(
                "CREATE TABLE local_workspaces_v2 ("
                "id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, path TEXT NOT NULL, "
                "label TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL, "
                "UNIQUE (principal_id, path))"
            )
            await conn.execute(
                "INSERT INTO local_workspaces_v2 "
                "(id, principal_id, path, label, created_at, last_used_at) "
                "SELECT id, ?, path, label, created_at, last_used_at FROM local_workspaces",
                (LOCAL_OWNER_PRINCIPAL_ID,),
            )
            await conn.execute("DROP TABLE local_workspaces")
            await conn.execute("ALTER TABLE local_workspaces_v2 RENAME TO local_workspaces")
            await conn.execute("RELEASE principal_scoped_workspaces")
        except BaseException:
            await conn.execute("ROLLBACK TO principal_scoped_workspaces")
            await conn.execute("RELEASE principal_scoped_workspaces")
            raise

    @staticmethod
    async def _ensure_principal_scoped_commands(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_commands)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "principal_id" in columns:
            return
        await conn.execute("SAVEPOINT principal_scoped_commands")
        try:
            await conn.execute(
                "CREATE TABLE local_commands_v2 ("
                "principal_id TEXT NOT NULL, id TEXT NOT NULL, command_type TEXT NOT NULL, "
                "client_message_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
                "run_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, "
                "PRIMARY KEY (principal_id, id), "
                "FOREIGN KEY (run_id) REFERENCES local_runs(id))"
            )
            await conn.execute(
                "INSERT INTO local_commands_v2 "
                "(principal_id, id, command_type, client_message_id, payload_json, "
                "run_id, created_at) "
                "SELECT ?, id, command_type, client_message_id, payload_json, run_id, "
                "created_at FROM local_commands",
                (LOCAL_OWNER_PRINCIPAL_ID,),
            )
            await conn.execute("DROP TABLE local_commands")
            await conn.execute("ALTER TABLE local_commands_v2 RENAME TO local_commands")
            await conn.execute("RELEASE principal_scoped_commands")
        except BaseException:
            await conn.execute("ROLLBACK TO principal_scoped_commands")
            await conn.execute("RELEASE principal_scoped_commands")
            raise

    @staticmethod
    async def _ensure_generic_commands(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("PRAGMA table_info(local_commands)")
        columns = {row[1] for row in await cursor.fetchall()}
        schema = await (
            await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'local_commands'"
            )
        ).fetchone()
        table_sql = str(schema[0] if schema else "").upper()
        if "response_json" in columns and "RUN_ID TEXT NOT NULL" not in table_sql:
            return
        await conn.execute("SAVEPOINT generic_commands")
        try:
            await conn.execute(
                "CREATE TABLE local_commands_v4 ("
                "principal_id TEXT NOT NULL, id TEXT NOT NULL, command_type TEXT NOT NULL, "
                "client_message_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
                "response_json TEXT NOT NULL DEFAULT '{}', run_id TEXT, "
                "created_at TEXT NOT NULL, PRIMARY KEY (principal_id, id), "
                "FOREIGN KEY (run_id) REFERENCES local_runs(id))"
            )
            response_expression = "response_json" if "response_json" in columns else "'{}'"
            await conn.execute(
                "INSERT INTO local_commands_v4 "
                "(principal_id, id, command_type, client_message_id, payload_json, "
                "response_json, run_id, created_at) "
                "SELECT principal_id, id, command_type, client_message_id, payload_json, "
                f"{response_expression}, run_id, created_at FROM local_commands"
            )
            await conn.execute("DROP TABLE local_commands")
            await conn.execute("ALTER TABLE local_commands_v4 RENAME TO local_commands")
            await conn.execute("RELEASE generic_commands")
        except BaseException:
            await conn.execute("ROLLBACK TO generic_commands")
            await conn.execute("RELEASE generic_commands")
            raise

    @staticmethod
    async def _ensure_run_job_principals(conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute(
            "SELECT jobs.id, jobs.input_json, runs.principal_id "
            "FROM local_run_jobs AS jobs "
            "JOIN local_runs AS runs ON runs.id = jobs.run_id"
        )
        for job_id, raw_input, principal_id in await cursor.fetchall():
            try:
                payload = json.loads(raw_input or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("principal_id"):
                continue
            payload["principal_id"] = principal_id
            await conn.execute(
                "UPDATE local_run_jobs SET input_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False, default=str), job_id),
            )

    @staticmethod
    async def _ensure_event_sequence_index(conn: aiosqlite.Connection) -> None:
        """Repair legacy duplicate sequences, then enforce one sequence per run."""
        cursor = await conn.execute(
            "SELECT run_id FROM local_events GROUP BY run_id HAVING COUNT(*) != COUNT(DISTINCT seq)"
        )
        for row in await cursor.fetchall():
            run_id = row[0]
            events = await (
                await conn.execute(
                    "SELECT id FROM local_events WHERE run_id = ? ORDER BY seq, created_at, id",
                    (run_id,),
                )
            ).fetchall()
            # Use temporary negative values so this migration also remains safe
            # if a partially migrated database already has a unique index.
            await conn.executemany(
                "UPDATE local_events SET seq = ? WHERE id = ?",
                [(-(index + 1), event[0]) for index, event in enumerate(events)],
            )
            await conn.executemany(
                "UPDATE local_events SET seq = ? WHERE id = ?",
                [((index + 1), event[0]) for index, event in enumerate(events)],
            )
        await conn.execute("DROP INDEX IF EXISTS idx_local_events_run_seq")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_events_run_seq "
            "ON local_events(run_id, seq)"
        )

    async def close(self) -> None:
        await self._conn.close()

    @contextmanager
    def bind_execution_lease(
        self,
        *,
        job_id: str,
        run_id: str,
        lease_owner: str,
        lease_generation: int,
    ):
        lease = ExecutionLease(job_id, run_id, lease_owner, lease_generation)
        token = _CURRENT_EXECUTION_LEASE.set(lease)
        try:
            yield lease
        finally:
            _CURRENT_EXECUTION_LEASE.reset(token)

    @staticmethod
    def current_execution_lease() -> ExecutionLease | None:
        return _CURRENT_EXECUTION_LEASE.get()

    @staticmethod
    async def _require_tool_receipt_in_run_uncommitted(
        conn: aiosqlite.Connection,
        *,
        operation_id: str,
        run_id: str,
    ) -> None:
        receipt = await (
            await conn.execute(
                "SELECT run_id FROM local_tool_receipts WHERE operation_id = ?",
                (operation_id,),
            )
        ).fetchone()
        if receipt is None:
            raise ToolReceiptStateError(f"parent tool receipt {operation_id} does not exist")
        if str(receipt["run_id"]) != run_id:
            raise ToolReceiptStateError(
                f"parent tool receipt {operation_id} must belong to the same run"
            )

    @asynccontextmanager
    async def run_write_transaction(
        self,
        run_id: str,
        *,
        lease: ExecutionLease | None = None,
    ):
        active_lease = lease or _CURRENT_EXECUTION_LEASE.get()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if active_lease is not None:
                    if active_lease.run_id != run_id:
                        raise LeaseFenceError(
                            f"lease for {active_lease.run_id} cannot write run {run_id}"
                        )
                    row = await (
                        await conn.execute(
                            "SELECT 1 FROM local_run_jobs WHERE id = ? AND run_id = ? "
                            "AND status = 'leased' AND lease_owner = ? "
                            "AND lease_generation = ? AND quarantined_at IS NULL "
                            "AND lease_expires_at > ?",
                            (
                                active_lease.job_id,
                                active_lease.run_id,
                                active_lease.lease_owner,
                                active_lease.lease_generation,
                                _now(),
                            ),
                        )
                    ).fetchone()
                    if row is None:
                        raise LeaseFenceError(
                            f"run {run_id} lease generation {active_lease.lease_generation} is stale"
                        )
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    # --- model services ---

    async def reserve_model_call(
        self,
        *,
        run_id: str,
        execution_attempt_id: str,
        model: str,
        max_calls: int,
        purpose: str = "agent",
        parent_tool_operation_id: str | None = None,
        logical_call_id: str | None = None,
        retry_attempt: int = 0,
    ) -> dict[str, Any]:
        """Atomically reserve one durable model-call slot for a run."""
        if purpose not in {
            "agent",
            "approval_review",
            "clarification_review",
            "completion_review",
            "title_generation",
            "summarization",
        }:
            raise ValueError("model call purpose is invalid")
        async with self.run_write_transaction(run_id) as conn:
            if parent_tool_operation_id is not None:
                await self._require_tool_receipt_in_run_uncommitted(
                    conn,
                    operation_id=parent_tool_operation_id,
                    run_id=run_id,
                )
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) AS total_count, "
                    "COALESCE(SUM(CASE WHEN purpose = ? THEN 1 ELSE 0 END), 0) AS purpose_count "
                    "FROM local_model_calls WHERE run_id = ?",
                    (purpose, run_id),
                )
            ).fetchone()
            call_index = int(row["total_count"] if row is not None else 0) + 1
            purpose_index = int(row["purpose_count"] if row is not None else 0) + 1
            if purpose_index > max(1, int(max_calls)):
                raise ModelCallBudgetExceeded(
                    f"{purpose} model call budget exhausted for run {run_id}: {max_calls}"
                )
            call_id = _new_id("model_call")
            record = {
                "id": call_id,
                "run_id": run_id,
                "execution_attempt_id": execution_attempt_id,
                "call_index": call_index,
                "model": model,
                "purpose": purpose,
                "parent_tool_operation_id": parent_tool_operation_id,
                "logical_call_id": logical_call_id or call_id,
                "retry_attempt": max(0, int(retry_attempt)),
                "status": "reserved",
                "created_at": _now(),
            }
            await conn.execute(
                "INSERT INTO local_model_calls "
                "(id, run_id, execution_attempt_id, call_index, model, purpose, "
                "parent_tool_operation_id, logical_call_id, retry_attempt, status, created_at) "
                "VALUES (:id, :run_id, :execution_attempt_id, :call_index, :model, :purpose, "
                ":parent_tool_operation_id, :logical_call_id, :retry_attempt, :status, :created_at)",
                record,
            )
        return record

    async def record_sandbox_process(
        self,
        *,
        run_id: str,
        execution_attempt_id: str,
        pid: int,
        process_started_at: str,
        settings_path: str,
    ) -> str:
        """Durably note a sandbox launcher this attempt is responsible for.

        Written before the command can outlive us so that a Runtime killed mid
        command leaves the next one something to act on. ``process_started_at``
        and ``settings_path`` are what later prove the pid was not recycled.
        """

        now = _now()
        record = {
            "id": _new_id("sbx"),
            "run_id": run_id,
            "execution_attempt_id": execution_attempt_id,
            "pid": int(pid),
            "process_started_at": process_started_at,
            "settings_path": settings_path,
            "status": "running",
            "created_at": now,
            "updated_at": now,
        }
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute(
                "INSERT INTO local_sandbox_processes "
                "(id, run_id, execution_attempt_id, pid, process_started_at, settings_path, "
                "status, created_at, updated_at) "
                "VALUES (:id, :run_id, :execution_attempt_id, :pid, :process_started_at, "
                ":settings_path, :status, :created_at, :updated_at)",
                record,
            )
            await conn.commit()
        return str(record["id"])

    async def forget_sandbox_process(self, sandbox_process_id: str) -> None:
        """Drop the record for a launcher that exited under our supervision."""

        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute(
                "DELETE FROM local_sandbox_processes WHERE id = ?",
                (sandbox_process_id,),
            )
            await conn.commit()

    async def list_reapable_sandbox_processes(
        self,
        *,
        include_running: bool = False,
    ) -> list[dict[str, Any]]:
        """Sandbox records that no live execution is supervising any more.

        ``include_running`` is for boot: a row still marked running when the
        Runtime has only just started belongs to the process that died, because
        this one has not spawned anything yet.
        """

        statuses = ("orphaned", "running") if include_running else ("orphaned",)
        placeholders = ", ".join("?" for _ in statuses)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            cursor = await conn.execute(
                "SELECT * FROM local_sandbox_processes "
                f"WHERE status IN ({placeholders}) ORDER BY created_at, id",
                statuses,
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def settle_sandbox_process(self, sandbox_process_id: str, *, status: str) -> None:
        """Record what the reaper found, so a record is never acted on twice."""

        if status not in {"reaped", "gone", "stale"}:
            raise ValueError(f"sandbox process cannot settle as {status!r}")
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute(
                "UPDATE local_sandbox_processes SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), sandbox_process_id),
            )
            await conn.commit()

    async def list_model_calls_for_run(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_model_calls WHERE run_id = ? ORDER BY call_index",
            (run_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_model_call_output(self, *, run_id: str, call_id: str) -> None:
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "UPDATE local_model_calls SET status = 'streaming', output_started = 1, "
                "first_output_at = COALESCE(first_output_at, ?) "
                "WHERE id = ? AND run_id = ? AND status IN ('reserved', 'streaming')",
                (_now(), call_id, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"model call {call_id} cannot record output")

    async def settle_model_call(
        self,
        *,
        run_id: str,
        call_id: str,
        provider_request_id: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        usage_known = input_tokens is not None or output_tokens is not None
        status = "completed" if usage_known else "completed_unmetered"
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "UPDATE local_model_calls SET status = ?, provider_request_id = ?, "
                "input_tokens = ?, output_tokens = ?, completed_at = ? "
                "WHERE id = ? AND run_id = ? AND status IN ('reserved', 'streaming')",
                (
                    status,
                    provider_request_id,
                    input_tokens,
                    output_tokens,
                    _now(),
                    call_id,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"model call {call_id} cannot be settled twice")

    async def fail_model_call(
        self,
        *,
        run_id: str,
        call_id: str,
        outcome_unknown: bool,
        error_code: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        status = "outcome_unknown" if outcome_unknown else "failed"
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "UPDATE local_model_calls SET status = ?, error_code = ?, "
                "provider_request_id = ?, completed_at = ? "
                "WHERE id = ? AND run_id = ? AND status IN ('reserved', 'streaming')",
                (status, error_code, provider_request_id, _now(), call_id, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"model call {call_id} cannot be failed")

    async def model_usage_summary(self, run_id: str) -> dict[str, int]:
        row = await (
            await self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "SUM(CASE WHEN status = 'completed_unmetered' THEN 1 ELSE 0 END) "
                "AS unmetered_calls, "
                "SUM(CASE WHEN status = 'outcome_unknown' THEN 1 ELSE 0 END) "
                "AS outcome_unknown_calls, COUNT(*) AS model_calls "
                "FROM local_model_calls WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return {
            key: int(row[key] or 0)
            for key in (
                "input_tokens",
                "output_tokens",
                "unmetered_calls",
                "outcome_unknown_calls",
                "model_calls",
            )
        }

    async def execution_settlement_snapshot(self, run_id: str) -> dict[str, Any]:
        """Read the authoritative records needed to settle one execution."""
        model_rows = await (
            await self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM local_model_calls "
                "WHERE run_id = ? GROUP BY status ORDER BY status",
                (run_id,),
            )
        ).fetchall()
        tool_rows = await (
            await self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM local_tool_receipts "
                "WHERE run_id = ? GROUP BY status ORDER BY status",
                (run_id,),
            )
        ).fetchall()
        draft = await (
            await self._conn.execute(
                "SELECT * FROM local_assistant_drafts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        artifact_count_row = await (
            await self._conn.execute(
                "SELECT COUNT(*) AS count FROM local_artifacts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        artifacts = await (
            await self._conn.execute(
                "SELECT id, kind, content_type, bytes FROM local_artifacts "
                "WHERE run_id = ? ORDER BY created_at, id LIMIT ?",
                (run_id, MAX_SETTLEMENT_ARTIFACT_REFS),
            )
        ).fetchall()
        verification = await (
            await self._conn.execute(
                "SELECT operation_id, status, result_hash FROM local_tool_receipts "
                "WHERE run_id = ? AND tool_name = 'task.verify' "
                "ORDER BY created_at DESC, operation_id DESC LIMIT 1",
                (run_id,),
            )
        ).fetchone()
        return {
            "assistant": dict(draft) if draft is not None else None,
            "usage": await self.model_usage_summary(run_id),
            "model_statuses": {str(row["status"]): int(row["count"]) for row in model_rows},
            "tool_statuses": {str(row["status"]): int(row["count"]) for row in tool_rows},
            "artifacts": {
                "count": int(artifact_count_row["count"] if artifact_count_row else 0),
                "items": [dict(row) for row in artifacts],
                "truncated": int(artifact_count_row["count"] if artifact_count_row else 0)
                > len(artifacts),
            },
            "verification": dict(verification) if verification is not None else None,
        }

    async def update_assistant_draft(
        self,
        *,
        run_id: str,
        message_key: str,
        content: str,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist one fully assembled top-level assistant model round."""
        now = _now()
        async with self.run_write_transaction(run_id) as conn:
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_assistant_drafts WHERE run_id = ?",
                    (run_id,),
                )
            ).fetchone()
            if existing is not None and existing["message_key"] == message_key:
                return dict(existing)
            revision = int(existing["revision"] if existing is not None else 0) + 1
            created_at = str(existing["created_at"] if existing is not None else now)
            await conn.execute(
                "INSERT INTO local_assistant_drafts "
                "(run_id, revision, message_key, content, tool_calls_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET revision = excluded.revision, "
                "message_key = excluded.message_key, content = excluded.content, "
                "tool_calls_json = excluded.tool_calls_json, updated_at = excluded.updated_at",
                (
                    run_id,
                    revision,
                    message_key,
                    content,
                    json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":"), default=str),
                    created_at,
                    now,
                ),
            )
        return {
            "run_id": run_id,
            "revision": revision,
            "message_key": message_key,
            "content": content,
            "tool_calls_json": json.dumps(
                tool_calls,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
            "created_at": created_at,
            "updated_at": now,
        }

    async def commit_assistant_round(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Append one complete model round exactly once across execution replay."""
        round_id = str(payload.get("round_id") or "")
        if not round_id:
            raise ValueError("assistant round requires round_id")
        payload_json = _encode_payload(payload)
        async with self.run_write_transaction(run_id) as conn:
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_events WHERE run_id = ? "
                    "AND event_type = 'assistant.round.committed' "
                    "AND json_extract(payload_json, '$.round_id') = ? LIMIT 1",
                    (run_id, round_id),
                )
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                if str(record["payload_json"]) != payload_json:
                    committed = _json_payload(record["payload_json"])
                    # An approved edit creates a replacement tool-call id while
                    # replaying the same model round. That routing identity is
                    # allowed to differ; display text and summary are immutable.
                    if any(
                        committed.get(key) != payload.get(key)
                        for key in ("text", "reasoning_summary")
                    ):
                        raise RunResultConflictError(
                            f"assistant round {round_id} was already committed differently"
                        )
                return record, False
            committed_at = _now()
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                "assistant.round.committed",
                payload_json=payload_json,
                created_at=committed_at,
            )
            await self._touch_thread_for_run_event_uncommitted(
                conn,
                run_id=run_id,
                change_type="assistant.round.committed",
                event_high_watermark=int(event["seq"]),
                changed_at=committed_at,
            )
            return event, True

    # --- durable tool execution receipts ---

    @staticmethod
    def _subagent_invocation_projection(record: dict[str, Any]) -> dict[str, Any]:
        receipt_status = str(record["status"])
        _event_type, status = _SUBAGENT_EVENT_BY_RECEIPT_STATUS[receipt_status]
        arguments = _json_payload(record.get("arguments_json"))
        return {
            "operation_id": str(record["operation_id"]),
            "parent_run_id": str(record["run_id"]),
            "parent_operation_id": (
                str(record["parent_operation_id"])
                if record.get("parent_operation_id") is not None
                else None
            ),
            "tool_call_id": str(record["tool_call_id"]),
            "subagent_type": str(arguments.get("subagent_type") or ""),
            "description": str(arguments.get("description") or ""),
            "status": status,
            "receipt_status": receipt_status,
            "attempt_count": int(record.get("attempt_count") or 0),
            "usage": {
                "model_calls": int(record.get("usage_model_calls") or 0),
                "input_tokens": int(record.get("usage_input_tokens") or 0),
                "output_tokens": int(record.get("usage_output_tokens") or 0),
                "unmetered_calls": int(record.get("usage_unmetered_calls") or 0),
                "outcome_unknown_calls": int(record.get("usage_outcome_unknown_calls") or 0),
            },
            "error_type": (
                str(record["error_type"]) if record.get("error_type") is not None else None
            ),
            "created_at": str(record["created_at"]),
            "started_at": (
                str(record["started_at"]) if record.get("started_at") is not None else None
            ),
            "completed_at": (
                str(record["completed_at"]) if record.get("completed_at") is not None else None
            ),
            "updated_at": str(record["updated_at"]),
        }

    @staticmethod
    async def _subagent_invocations_uncommitted(
        conn: aiosqlite.Connection,
        run_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        rows = await (
            await conn.execute(
                "SELECT r.*, COUNT(m.id) AS usage_model_calls, "
                "COALESCE(SUM(m.input_tokens), 0) AS usage_input_tokens, "
                "COALESCE(SUM(m.output_tokens), 0) AS usage_output_tokens, "
                "COALESCE(SUM(CASE WHEN m.status = 'completed_unmetered' THEN 1 ELSE 0 END), 0) "
                "AS usage_unmetered_calls, "
                "COALESCE(SUM(CASE WHEN m.status = 'outcome_unknown' THEN 1 ELSE 0 END), 0) "
                "AS usage_outcome_unknown_calls "
                "FROM local_tool_receipts r LEFT JOIN local_model_calls m "
                "ON m.parent_tool_operation_id = r.operation_id "
                f"WHERE r.tool_name = 'task' AND r.run_id IN ({placeholders}) "
                "GROUP BY r.operation_id ORDER BY r.created_at, r.operation_id",
                run_ids,
            )
        ).fetchall()
        return [LocalStore._subagent_invocation_projection(dict(row)) for row in rows]

    async def list_subagent_invocations_for_runs(
        self,
        run_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Return receipt-owned SubAgent snapshots for a batch of parent Runs."""
        normalized_run_ids = list(dict.fromkeys(str(run_id) for run_id in run_ids if run_id))
        return await self._subagent_invocations_uncommitted(self._conn, normalized_run_ids)

    @staticmethod
    async def _touch_thread_for_run_event_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        change_type: str,
        event_high_watermark: int,
        changed_at: str,
    ) -> None:
        """Publish a Run event to P4 without changing assistant content or status."""
        run = await (
            await conn.execute(
                "SELECT principal_id, thread_id, assistant_item_id FROM local_runs WHERE id = ?",
                (run_id,),
            )
        ).fetchone()
        if run is None or not run["thread_id"] or not run["assistant_item_id"]:
            return
        item_cursor = await conn.execute(
            "UPDATE local_thread_items SET event_high_watermark = "
            "MAX(event_high_watermark, ?), version = version + 1, updated_at = ? "
            "WHERE id = ? AND run_id = ?",
            (event_high_watermark, changed_at, run["assistant_item_id"], run_id),
        )
        if item_cursor.rowcount != 1:
            raise RunResultConflictError(f"run {run_id} is missing its assistant projection")
        thread = await (
            await conn.execute(
                "SELECT version FROM local_threads WHERE id = ? AND principal_id = ?",
                (run["thread_id"], run["principal_id"]),
            )
        ).fetchone()
        if thread is None:
            raise RunResultConflictError(f"run {run_id} is missing its thread projection")
        thread_version = int(thread["version"]) + 1
        await conn.execute(
            "UPDATE local_threads SET version = ?, updated_at = ? WHERE id = ?",
            (thread_version, changed_at, run["thread_id"]),
        )
        await conn.execute(
            "INSERT INTO local_thread_changes "
            "(principal_id, thread_id, thread_version, change_type, run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run["principal_id"],
                run["thread_id"],
                thread_version,
                change_type,
                run_id,
                changed_at,
            ),
        )

    async def _append_subagent_receipt_event_uncommitted(
        self,
        conn: aiosqlite.Connection,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        if str(receipt.get("tool_name") or "") != "task":
            return None
        usage = await (
            await conn.execute(
                "SELECT COUNT(*) AS usage_model_calls, "
                "COALESCE(SUM(input_tokens), 0) AS usage_input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS usage_output_tokens, "
                "COALESCE(SUM(CASE WHEN status = 'completed_unmetered' THEN 1 ELSE 0 END), 0) "
                "AS usage_unmetered_calls, "
                "COALESCE(SUM(CASE WHEN status = 'outcome_unknown' THEN 1 ELSE 0 END), 0) "
                "AS usage_outcome_unknown_calls FROM local_model_calls "
                "WHERE parent_tool_operation_id = ?",
                (receipt["operation_id"],),
            )
        ).fetchone()
        projected = self._subagent_invocation_projection(
            {**receipt, **(dict(usage) if usage is not None else {})}
        )
        event_type, _status = _SUBAGENT_EVENT_BY_RECEIPT_STATUS[str(receipt["status"])]
        event = await self._append_event_uncommitted(
            conn,
            str(receipt["run_id"]),
            event_type,
            payload_json=_encode_payload(projected),
            created_at=str(receipt["updated_at"]),
        )
        await self._touch_thread_for_run_event_uncommitted(
            conn,
            run_id=str(receipt["run_id"]),
            change_type=event_type,
            event_high_watermark=int(event["seq"]),
            changed_at=str(receipt["updated_at"]),
        )
        return event

    async def _mark_running_tool_receipts_outcome_unknown_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        execution_attempt_id: str,
        error_type: str,
        now: str,
    ) -> None:
        """Fence a lost attempt and project every transitioned task atomically."""
        running = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                "AND execution_attempt_id = ? AND status = 'running' "
                "ORDER BY created_at, operation_id",
                (run_id, execution_attempt_id),
            )
        ).fetchall()
        if not running:
            return
        await conn.execute(
            "UPDATE local_tool_receipts SET status = 'outcome_unknown', "
            "error_type = ?, updated_at = ?, completed_at = ? "
            "WHERE run_id = ? AND execution_attempt_id = ? AND status = 'running'",
            (error_type, now, now, run_id, execution_attempt_id),
        )
        for row in running:
            receipt = {
                **dict(row),
                "status": "outcome_unknown",
                "error_type": error_type,
                "updated_at": now,
                "completed_at": now,
            }
            await self._append_subagent_receipt_event_uncommitted(conn, receipt)

    async def _cancel_unstarted_tool_receipts_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        canceled_at: str,
        error_type: str = "RunCanceled",
    ) -> None:
        """Close queued or paused tools when their parent Run can no longer resume them."""
        open_receipts = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                "AND status IN ('prepared', 'paused') ORDER BY created_at, operation_id",
                (run_id,),
            )
        ).fetchall()
        if not open_receipts:
            return
        await conn.execute(
            "UPDATE local_tool_receipts SET status = 'canceled', "
            "error_type = ?, completed_at = ?, updated_at = ? "
            "WHERE run_id = ? AND status IN ('prepared', 'paused')",
            (error_type, canceled_at, canceled_at, run_id),
        )
        for row in open_receipts:
            await self._append_subagent_receipt_event_uncommitted(
                conn,
                {
                    **dict(row),
                    "status": "canceled",
                    "error_type": error_type,
                    "completed_at": canceled_at,
                    "updated_at": canceled_at,
                },
            )

    async def prepare_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        execution_attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments_hash: str,
        arguments_json: str,
        risk: str,
        tool_version: str = "",
        execution_namespace: str = "main",
        parent_operation_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.run_write_transaction(run_id) as conn:
            if parent_operation_id is not None:
                if parent_operation_id == operation_id:
                    raise ToolReceiptStateError(f"tool receipt {operation_id} cannot parent itself")
                await self._require_tool_receipt_in_run_uncommitted(
                    conn,
                    operation_id=parent_operation_id,
                    run_id=run_id,
                )
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                    "AND execution_namespace = ? AND tool_call_id = ?",
                    (run_id, execution_namespace, tool_call_id),
                )
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                existing_parent_operation_id = record.get("parent_operation_id")
                if (
                    record["operation_id"] != operation_id
                    or record["execution_namespace"] != execution_namespace
                    or (
                        parent_operation_id is not None
                        and existing_parent_operation_id not in {None, parent_operation_id}
                    )
                    or record["tool_name"] != tool_name
                    or record["tool_version"] != tool_version
                    or record["arguments_hash"] != arguments_hash
                ):
                    raise ToolReceiptConflictError(
                        f"tool call {tool_call_id} was reused with a different operation identity"
                    )
                if parent_operation_id is not None and existing_parent_operation_id is None:
                    await conn.execute(
                        "UPDATE local_tool_receipts SET parent_operation_id = ? "
                        "WHERE operation_id = ? AND parent_operation_id IS NULL",
                        (parent_operation_id, operation_id),
                    )
                    record["parent_operation_id"] = parent_operation_id
                return record
            now = _now()
            await conn.execute(
                "INSERT INTO local_tool_receipts "
                "(operation_id, run_id, execution_attempt_id, execution_namespace, "
                "parent_operation_id, "
                "tool_call_id, tool_name, "
                "tool_version, arguments_hash, arguments_json, risk, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
                (
                    operation_id,
                    run_id,
                    execution_attempt_id,
                    execution_namespace,
                    parent_operation_id,
                    tool_call_id,
                    tool_name,
                    tool_version,
                    arguments_hash,
                    arguments_json,
                    risk,
                    now,
                    now,
                ),
            )
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def record_tool_review(
        self,
        *,
        operation_id: str,
        run_id: str,
        decision: str,
        source: str,
        reason: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"allow", "ask", "deny"}:
            raise ValueError("tool review decision is invalid")
        if source not in {"rule", "llm", "fallback", "user", "run_grant"}:
            raise ValueError("tool review source is invalid")
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown tool receipt: {operation_id}")
            record = dict(row)
            if record.get("review_decision") is not None:
                if (
                    record.get("review_decision") != decision
                    or record.get("review_source") != source
                    or str(record.get("review_reason") or "") != reason
                    or record.get("review_model") != model
                ):
                    raise ToolReceiptStateError(
                        f"tool receipt {operation_id} already has a different review decision"
                    )
                return record
            reviewed_at = _now()
            await conn.execute(
                "UPDATE local_tool_receipts SET review_decision = ?, review_source = ?, "
                "review_reason = ?, review_model = ?, reviewed_at = ?, updated_at = ? "
                "WHERE operation_id = ? AND run_id = ? AND review_decision IS NULL",
                (
                    decision,
                    source,
                    reason,
                    model,
                    reviewed_at,
                    reviewed_at,
                    operation_id,
                    run_id,
                ),
            )
            updated = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            assert updated is not None
            return dict(updated)

    async def begin_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        execution_attempt_id: str,
    ) -> dict[str, Any]:
        execution_lease = _CURRENT_EXECUTION_LEASE.get()
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            if row is None:
                raise ToolReceiptStateError(f"tool receipt {operation_id} is missing")
            record = dict(row)
            status = str(record["status"])
            if status in {"completed", "failed", "rejected", "canceled"}:
                return record
            if status in {"running", "outcome_unknown"}:
                raise ToolOutcomeUnknownError(
                    f"tool operation {operation_id} has unresolved outcome {status}"
                )
            if status not in {"prepared", "paused"}:
                raise ToolReceiptStateError(
                    f"tool operation {operation_id} cannot start from {status}"
                )
            now = _now()
            if execution_lease is not None:
                job = await (
                    await conn.execute(
                        "SELECT cancel_requested_at FROM local_run_jobs "
                        "WHERE id = ? AND run_id = ? AND status = 'leased' "
                        "AND lease_owner = ? AND lease_generation = ?",
                        (
                            execution_lease.job_id,
                            run_id,
                            execution_lease.lease_owner,
                            execution_lease.lease_generation,
                        ),
                    )
                ).fetchone()
                if job is None:
                    raise LeaseFenceError("tool execution lease is stale")
                if job["cancel_requested_at"] is not None:
                    if str(record["tool_name"]) == "task":
                        await self._settle_task_descendants_uncommitted(
                            conn,
                            run_id=run_id,
                            operation_id=operation_id,
                            parent_status="canceled",
                            now=now,
                        )
                    await conn.execute(
                        "UPDATE local_tool_receipts SET status = 'canceled', "
                        "error_type = 'RunCanceledBeforeToolStart', completed_at = ?, "
                        "updated_at = ? WHERE operation_id = ? AND run_id = ? "
                        "AND status IN ('prepared', 'paused')",
                        (now, now, operation_id, run_id),
                    )
                    canceled = await (
                        await conn.execute(
                            "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                            (operation_id,),
                        )
                    ).fetchone()
                    assert canceled is not None
                    record = dict(canceled)
                    await self._append_subagent_receipt_event_uncommitted(conn, record)
                    return record
            await conn.execute(
                "UPDATE local_tool_receipts SET status = 'running', "
                "execution_attempt_id = ?, attempt_count = attempt_count + 1, "
                "started_at = COALESCE(started_at, ?), updated_at = ? "
                "WHERE operation_id = ? AND run_id = ?",
                (execution_attempt_id, now, now, operation_id, run_id),
            )
            updated = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert updated is not None
            record = dict(updated)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def settle_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        status: str,
        result_json: str | None = None,
        result_hash: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        if status not in {
            "paused",
            "completed",
            "failed",
            "outcome_unknown",
            "rejected",
            "canceled",
        }:
            raise ValueError(f"invalid tool receipt status: {status}")
        async with self.run_write_transaction(run_id) as conn:
            now = _now()
            cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, result_hash = ?, "
                "error_type = ?, updated_at = ?, completed_at = CASE WHEN ? = 'paused' "
                "THEN completed_at ELSE ? END WHERE operation_id = ? AND run_id = ? "
                "AND (status = 'running' OR (? IN ('rejected', 'failed') "
                "AND status = 'prepared') OR (? = 'canceled' "
                "AND status IN ('prepared', 'paused')))",
                (
                    status,
                    result_json,
                    result_hash,
                    error_type,
                    now,
                    status,
                    now,
                    operation_id,
                    run_id,
                    status,
                    status,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolReceiptStateError(f"tool operation {operation_id} is not running")
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def settle_task_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        status: str,
        result_json: str | None = None,
        result_hash: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        """Settle a task and fence every descendant receipt in the same transaction."""
        if status not in {"completed", "failed", "canceled"}:
            raise ValueError(f"invalid task receipt status: {status}")
        async with self.run_write_transaction(run_id) as conn:
            parent = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            if parent is None or str(parent["tool_name"]) != "task":
                raise ToolReceiptStateError(f"task receipt {operation_id} is missing")

            now = _now()
            await self._settle_task_descendants_uncommitted(
                conn,
                run_id=run_id,
                operation_id=operation_id,
                parent_status=status,
                now=now,
            )

            cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, result_hash = ?, "
                "error_type = ?, updated_at = ?, completed_at = ? "
                "WHERE operation_id = ? AND run_id = ? AND (status = 'running' OR "
                "(? = 'canceled' AND status IN ('prepared', 'paused')))",
                (
                    status,
                    result_json,
                    result_hash,
                    error_type,
                    now,
                    now,
                    operation_id,
                    run_id,
                    status,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolReceiptStateError(f"task operation {operation_id} is not running")
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def _settle_task_descendants_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        operation_id: str,
        parent_status: str,
        now: str,
    ) -> None:
        descendant_error = {
            "completed": "ParentSubagentCompleted",
            "failed": "ParentSubagentFailed",
            "canceled": "ParentSubagentCanceled",
        }[parent_status]
        descendants = await (
            await conn.execute(
                "WITH RECURSIVE descendants(operation_id, depth) AS ("
                "SELECT operation_id, 1 FROM local_tool_receipts "
                "WHERE run_id = ? AND parent_operation_id = ? UNION "
                "SELECT child.operation_id, descendants.depth + 1 "
                "FROM local_tool_receipts AS child JOIN descendants "
                "ON child.parent_operation_id = descendants.operation_id "
                "WHERE child.run_id = ? AND descendants.depth < 64"
                ") SELECT receipt.*, descendants.depth FROM local_tool_receipts AS receipt "
                "JOIN descendants ON descendants.operation_id = receipt.operation_id "
                "ORDER BY descendants.depth DESC, receipt.created_at, receipt.operation_id",
                (run_id, operation_id, run_id),
            )
        ).fetchall()
        for row in descendants:
            child = dict(row)
            child_status = str(child["status"])
            if child_status in {"prepared", "paused"}:
                settled_status = "canceled"
            elif child_status == "running":
                settled_status = "outcome_unknown"
            else:
                continue
            await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, error_type = ?, "
                "completed_at = ?, updated_at = ? WHERE operation_id = ? AND run_id = ?",
                (
                    settled_status,
                    descendant_error,
                    now,
                    now,
                    child["operation_id"],
                    run_id,
                ),
            )
            await self._append_subagent_receipt_event_uncommitted(
                conn,
                {
                    **child,
                    "status": settled_status,
                    "error_type": descendant_error,
                    "completed_at": now,
                    "updated_at": now,
                },
            )

    async def reconcile_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        decision: str,
        result_json: str | None = None,
        result_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an uncertain side effect without guessing or blind retry."""
        if decision not in {"confirmed_completed", "retry_not_executed", "abort"}:
            raise ValueError(f"invalid tool reconciliation decision: {decision}")
        status = {
            "confirmed_completed": "completed",
            "retry_not_executed": "prepared",
            "abort": "failed",
        }[decision]
        settled_result_json = None if decision == "retry_not_executed" else result_json
        settled_result_hash = None if decision == "retry_not_executed" else result_hash
        error_type = "ReconciledByUser" if decision == "abort" else None
        async with self.run_write_transaction(run_id) as conn:
            now = _now()
            cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, "
                "result_hash = ?, error_type = ?, updated_at = ?, "
                "completed_at = CASE WHEN ? = 'prepared' THEN NULL ELSE ? END "
                "WHERE operation_id = ? AND run_id = ? AND status = 'outcome_unknown'",
                (
                    status,
                    settled_result_json,
                    settled_result_hash,
                    error_type,
                    now,
                    status,
                    now,
                    operation_id,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolReceiptStateError(
                    f"tool operation {operation_id} is not awaiting reconciliation"
                )
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def tool_execution_cancel_requested(self, run_id: str) -> bool:
        """Fence a tool start against the currently leased run job."""
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None:
            return False
        if lease.run_id != run_id:
            raise LeaseFenceError("tool execution is missing its run job lease")
        row = await (
            await self._conn.execute(
                "SELECT status, lease_owner, lease_generation, lease_expires_at, "
                "cancel_requested_at FROM local_run_jobs WHERE id = ? AND run_id = ?",
                (lease.job_id, run_id),
            )
        ).fetchone()
        if (
            row is None
            or row["status"] != "leased"
            or row["lease_owner"] != lease.lease_owner
            or int(row["lease_generation"] or 0) != lease.lease_generation
            or str(row["lease_expires_at"] or "") <= _now()
        ):
            raise LeaseFenceError("tool execution lease is stale")
        return row["cancel_requested_at"] is not None

    async def get_tool_receipt(self, operation_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                (operation_id,),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_tool_receipts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_tool_receipts WHERE run_id = ? ORDER BY created_at, operation_id",
                (run_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def accept_child_run(
        self,
        *,
        parent_run_id: str,
        spawn_operation_id: str,
        goal: str,
        agent_definition: dict[str, Any],
        coordination: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically admit one Runtime-owned child Run and pending Job.

        The caller must be executing the matching ``child.spawn`` Tool Receipt
        under the parent's current job lease. ``spawn_operation_id`` is the
        replay key, so a process crash can rediscover the same child instead of
        creating a duplicate.
        """
        normalized_goal = goal.strip()
        if not normalized_goal or len(normalized_goal) > 32 * 1024:
            raise ValueError("child task is invalid")
        definition = _normalize_child_agent_definition(agent_definition)
        definition_json = _encode_payload(definition)
        async with self.run_write_transaction(parent_run_id) as conn:
            parent_row = await (
                await conn.execute("SELECT * FROM local_runs WHERE id = ?", (parent_run_id,))
            ).fetchone()
            if parent_row is None:
                raise KeyError(f"unknown parent run: {parent_run_id}")
            parent = dict(parent_row)
            normalized_coordination = _normalize_child_coordination(
                coordination,
                workspace_path=parent.get("workspace_path"),
            )
            policy = {
                "max_depth": MAX_DURABLE_CHILD_DEPTH,
                "max_children": MAX_DURABLE_CHILDREN_PER_RUN,
                **normalized_coordination,
            }
            policy_json = _encode_payload(policy)
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_runs WHERE spawn_operation_id = ?",
                    (spawn_operation_id,),
                )
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                metadata = _json_payload(record.get("metadata_json"))
                if (
                    record.get("run_kind") != "child"
                    or record.get("parent_run_id") != parent_run_id
                    or record.get("goal") != normalized_goal
                    or record.get("agent_definition_id") != definition["id"]
                    or record.get("agent_definition_version") != definition["version"]
                    or _encode_payload(metadata.get("_child_agent_definition") or {})
                    != definition_json
                    or _encode_payload(_json_payload(record.get("collaboration_policy_json")))
                    != policy_json
                ):
                    raise CommandConflictError(
                        f"spawn operation {spawn_operation_id} was reused with a different child specification"
                    )
                return record, False

            receipt = await (
                await conn.execute(
                    "SELECT tool_name, status, execution_attempt_id FROM local_tool_receipts "
                    "WHERE operation_id = ? AND run_id = ?",
                    (spawn_operation_id, parent_run_id),
                )
            ).fetchone()
            if (
                receipt is None
                or receipt["tool_name"] != "child.spawn"
                or receipt["status"] != "running"
            ):
                raise ToolReceiptStateError(
                    "child admission requires its running child.spawn receipt"
                )
            lease = _CURRENT_EXECUTION_LEASE.get()
            if lease is None:
                raise LeaseFenceError("child admission requires the parent execution lease")
            expected_attempt_id = f"{lease.job_id}:{lease.lease_generation}"
            if str(receipt["execution_attempt_id"]) != expected_attempt_id:
                raise LeaseFenceError("child.spawn receipt belongs to a stale execution attempt")
            if parent["status"] != "running":
                raise RunAdmissionError(
                    "child_parent_not_running",
                    "a child can only be admitted while its parent is running",
                )
            child_depth = int(parent.get("collaboration_depth") or 0) + 1
            if child_depth > MAX_DURABLE_CHILD_DEPTH:
                raise RunAdmissionError(
                    "child_depth_exceeded",
                    "durable child depth is exhausted for this Run",
                )
            child_count = int(
                (
                    await (
                        await conn.execute(
                            "SELECT COUNT(*) FROM local_runs "
                            "WHERE parent_run_id = ? AND run_kind = 'child'",
                            (parent_run_id,),
                        )
                    ).fetchone()
                )[0]
            )
            if child_count >= MAX_DURABLE_CHILDREN_PER_RUN:
                raise RunAdmissionError(
                    "child_fanout_exceeded",
                    "durable child fan-out is exhausted for this Run",
                )

            depends_on = normalized_coordination["depends_on"]
            if depends_on:
                placeholders = ",".join("?" for _ in depends_on)
                rows = await (
                    await conn.execute(
                        "SELECT id FROM local_runs WHERE parent_run_id = ? "
                        f"AND run_kind = 'child' AND id IN ({placeholders})",
                        (parent_run_id, *depends_on),
                    )
                ).fetchall()
                found = {str(row["id"]) for row in rows}
                missing = [run_id for run_id in depends_on if run_id not in found]
                if missing:
                    raise RunAdmissionError(
                        "child_dependency_invalid",
                        "child dependencies must be previously admitted siblings",
                    )

            quorum_group = normalized_coordination["quorum_group"]
            quorum_required = normalized_coordination["quorum_required"]
            if quorum_group is not None:
                existing_quorum = await (
                    await conn.execute(
                        "SELECT quorum_required FROM local_child_coordination "
                        "WHERE parent_run_id = ? AND completion_mode = 'quorum' "
                        "AND quorum_group = ? LIMIT 1",
                        (parent_run_id, quorum_group),
                    )
                ).fetchone()
                if existing_quorum is not None and int(existing_quorum[0]) != quorum_required:
                    raise RunAdmissionError(
                        "child_quorum_conflict",
                        "all children in a quorum group must use the same quorum_required",
                    )

            root_run_id = str(parent.get("root_run_id") or parent_run_id)
            for resource_key in normalized_coordination["resource_claims"]:
                owner = await (
                    await conn.execute(
                        "SELECT owner_run_id FROM local_collaboration_resource_claims "
                        "WHERE root_run_id = ? AND resource_key = ?",
                        (root_run_id, resource_key),
                    )
                ).fetchone()
                if owner is not None:
                    raise RunAdmissionError(
                        "child_resource_already_owned",
                        f"collaboration resource is already owned by {owner['owner_run_id']}",
                    )

            parent_metadata = _json_payload(parent.get("metadata_json"))
            child_metadata: dict[str, Any] = {
                "_child_agent_definition": definition,
                "_spawn_operation_id": spawn_operation_id,
            }
            attachments = parent_metadata.get("_attachments")
            if isinstance(attachments, list):
                child_metadata["_attachments"] = attachments
            child = self._new_run_record(
                principal_id=str(parent["principal_id"]),
                goal=normalized_goal,
                workspace_path=parent.get("workspace_path"),
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                settings=_json_payload(parent.get("settings_json")),
                metadata=child_metadata,
                mode=str(parent["mode"]),
                run_kind="child",
                agent_definition_id=str(definition["id"]),
                agent_definition_version=str(definition["version"]),
                collaboration_depth=child_depth,
                collaboration_policy=policy,
                spawn_operation_id=spawn_operation_id,
            )
            await self._insert_run(conn, child)
            await conn.execute(
                "INSERT INTO local_child_coordination "
                "(child_run_id, root_run_id, parent_run_id, completion_mode, quorum_group, "
                "quorum_required, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    child["id"],
                    root_run_id,
                    parent_run_id,
                    normalized_coordination["completion_mode"],
                    quorum_group,
                    quorum_required,
                    child["created_at"],
                ),
            )
            for dependency_run_id in depends_on:
                await conn.execute(
                    "INSERT INTO local_child_dependencies "
                    "(child_run_id, dependency_run_id, created_at) VALUES (?, ?, ?)",
                    (child["id"], dependency_run_id, child["created_at"]),
                )
            for resource_key in normalized_coordination["resource_claims"]:
                await conn.execute(
                    "INSERT INTO local_collaboration_resource_claims "
                    "(root_run_id, resource_key, owner_run_id, created_at) VALUES (?, ?, ?, ?)",
                    (root_run_id, resource_key, child["id"], child["created_at"]),
                )
            await conn.execute(
                "INSERT INTO local_run_inputs "
                "(run_id, input_id, virtual_path, original_name, media_type, bytes, "
                "sha256, blob_key, created_at) "
                "SELECT ?, input_id, virtual_path, original_name, media_type, bytes, "
                "sha256, blob_key, created_at FROM local_run_inputs WHERE run_id = ?",
                (child["id"], parent_run_id),
            )
            await conn.execute(
                "INSERT INTO run_plugin_bindings "
                "(run_id, plugin_id, version, digest, selection_source, required, command_id, "
                "action_catalog_hash, model_binding_json) "
                "SELECT ?, plugin_id, version, digest, selection_source, required, command_id, "
                "action_catalog_hash, model_binding_json FROM run_plugin_bindings WHERE run_id = ?",
                (child["id"], parent_run_id),
            )
            await self._insert_run_job(
                conn,
                self._new_run_job_record(
                    run_id=str(child["id"]),
                    kind="start",
                    input_payload=self._run_job_input(child),
                ),
            )
            spawned_payload = {
                "child_run_id": str(child["id"]),
                "parent_run_id": parent_run_id,
                "root_run_id": str(child["root_run_id"]),
                "agent_definition_id": str(child["agent_definition_id"]),
                "agent_definition_version": str(child["agent_definition_version"]),
                "collaboration_depth": child_depth,
                "goal": normalized_goal,
                "status": "queued",
                "spawn_operation_id": spawn_operation_id,
                **normalized_coordination,
                "created_at": str(child["created_at"]),
                "updated_at": str(child["updated_at"]),
            }
            event = await self._append_event_uncommitted(
                conn,
                parent_run_id,
                "child.spawned",
                payload_json=_encode_payload(spawned_payload),
                created_at=str(child["created_at"]),
            )
            await self._touch_thread_for_run_event_uncommitted(
                conn,
                run_id=parent_run_id,
                change_type="child.spawned",
                event_high_watermark=int(event["seq"]),
                changed_at=str(child["created_at"]),
            )
            return {**child, "_spawn_event": event}, True

    async def list_child_runs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    "r.parent_run_id = ? AND r.run_kind = 'child'",
                    "ORDER BY r.created_at, r.id",
                ),
                (run_id,),
            )
        ).fetchall()
        return [self._child_run_projection(dict(row)) for row in rows]

    async def child_run_for_spawn_operation(
        self,
        parent_run_id: str,
        spawn_operation_id: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    "r.parent_run_id = ? AND r.spawn_operation_id = ? AND r.run_kind = 'child'",
                ),
                (parent_run_id, spawn_operation_id),
            )
        ).fetchone()
        return self._child_run_projection(dict(row)) if row is not None else None

    async def list_child_runs_for_runs(
        self,
        parent_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(run_id) for run_id in parent_run_ids if run_id))
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        rows = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    f"r.parent_run_id IN ({placeholders}) AND r.run_kind = 'child'",
                    "ORDER BY r.parent_run_id, r.created_at, r.id",
                ),
                normalized_ids,
            )
        ).fetchall()
        return [self._child_run_projection(dict(row)) for row in rows]

    async def child_runs_for_parent(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(run_id) for run_id in child_run_ids if run_id))
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        rows = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    f"r.parent_run_id = ? AND r.run_kind = 'child' AND r.id IN ({placeholders})",
                ),
                (parent_run_id, *normalized_ids),
            )
        ).fetchall()
        by_id = {str(row["id"]): self._child_run_projection(dict(row)) for row in rows}
        missing = [run_id for run_id in normalized_ids if run_id not in by_id]
        if missing:
            raise KeyError(f"child run not found: {missing[0]}")
        return [by_id[run_id] for run_id in normalized_ids]

    @staticmethod
    def _child_run_snapshot_sql(where: str, suffix: str = "") -> str:
        return (
            "SELECT r.id, r.parent_run_id, r.root_run_id, r.run_kind, r.goal, r.status, "
            "r.agent_definition_id, r.agent_definition_version, r.collaboration_depth, "
            "r.collaboration_policy_json, r.spawn_operation_id, r.graph_thread_id, "
            "r.graph_checkpoint_id, r.created_at, r.updated_at, r.completed_at, "
            "COALESCE(c.completion_mode, 'required') AS completion_mode, "
            "c.quorum_group, c.quorum_required, "
            "COALESCE((SELECT json_group_array(dependency_run_id) FROM "
            "(SELECT dependency_run_id FROM local_child_dependencies "
            "WHERE child_run_id = r.id ORDER BY dependency_run_id)), '[]') AS depends_on_json, "
            "COALESCE((SELECT json_group_array(resource_key) FROM "
            "(SELECT resource_key FROM local_collaboration_resource_claims "
            "WHERE owner_run_id = r.id ORDER BY resource_key)), '[]') AS resource_claims_json, "
            "d.content AS result_text, "
            "(SELECT e.payload_json FROM local_events e WHERE e.run_id = r.id "
            "AND e.event_type IN ('run.waiting', 'run.completed', 'run.failed', "
            "'run.canceled', 'run.cleanup_required') ORDER BY e.seq DESC LIMIT 1) "
            "AS result_payload_json, "
            "(SELECT COUNT(*) FROM local_events e WHERE e.run_id = r.id) AS events_count "
            "FROM local_runs r LEFT JOIN local_assistant_drafts d ON d.run_id = r.id "
            "LEFT JOIN local_child_coordination c ON c.child_run_id = r.id "
            f"WHERE {where} {suffix}"
        )

    @staticmethod
    def _child_run_projection(row: dict[str, Any]) -> dict[str, Any]:
        payload = _json_payload(row.pop("result_payload_json", None))
        result_text = row.pop("result_text", None)
        collaboration_policy = _json_payload(row.pop("collaboration_policy_json", None))
        try:
            depends_on = json.loads(str(row.pop("depends_on_json", "[]")))
            resource_claims = json.loads(str(row.pop("resource_claims_json", "[]")))
        except (json.JSONDecodeError, TypeError):
            depends_on = []
            resource_claims = []
        projected = {
            **row,
            "collaboration_policy": collaboration_policy,
            "depends_on": depends_on if isinstance(depends_on, list) else [],
            "resource_claims": resource_claims if isinstance(resource_claims, list) else [],
            "quorum_required": (
                int(row["quorum_required"]) if row.get("quorum_required") is not None else None
            ),
            "result": (
                str(result_text)
                if row.get("status") == "completed" and result_text is not None
                else None
            ),
            "error": payload.get("error"),
            "error_type": payload.get("type"),
            "retryable": payload.get("retryable"),
            "input_tokens": int(payload.get("input_tokens") or 0),
            "output_tokens": int(payload.get("output_tokens") or 0),
            "model_calls": int(payload.get("model_calls") or 0),
        }
        return projected

    async def assert_workspace_resource_owner(
        self,
        *,
        run_id: str,
        requested_path: str,
    ) -> None:
        run = await (
            await self._conn.execute(
                "SELECT root_run_id, workspace_path FROM local_runs WHERE id = ?",
                (run_id,),
            )
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        root_run_id = str(run["root_run_id"] or run_id)
        has_claims = await (
            await self._conn.execute(
                "SELECT 1 FROM local_collaboration_resource_claims WHERE root_run_id = ? LIMIT 1",
                (root_run_id,),
            )
        ).fetchone()
        if has_claims is None:
            return
        resource_key = _normalize_workspace_resource_key(
            str(run["workspace_path"]) if run["workspace_path"] else None,
            requested_path,
        )
        owner = await (
            await self._conn.execute(
                "SELECT owner_run_id FROM local_collaboration_resource_claims "
                "WHERE root_run_id = ? AND resource_key = ?",
                (root_run_id, resource_key),
            )
        ).fetchone()
        if owner is not None and str(owner["owner_run_id"]) != run_id:
            raise RunAdmissionError(
                "collaboration_resource_not_owned",
                f"workspace resource {resource_key} is owned by another collaboration member",
            )

    async def has_foreign_workspace_resource_claims(self, run_id: str) -> bool:
        row = await (
            await self._conn.execute(
                "SELECT 1 FROM local_collaboration_resource_claims claims "
                "JOIN local_runs run ON run.root_run_id = claims.root_run_id "
                "WHERE run.id = ? AND claims.owner_run_id != ? LIMIT 1",
                (run_id, run_id),
            )
        ).fetchone()
        return row is not None

    async def collaboration_snapshot(self, root_run_id: str) -> dict[str, Any]:
        """Read one root collaboration at a single SQLite snapshot boundary."""
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            try:
                root_row = await (
                    await conn.execute(
                        "SELECT r.id, r.parent_run_id, r.root_run_id, r.run_kind, r.goal, "
                        "r.status, r.agent_definition_id, r.agent_definition_version, "
                        "r.graph_thread_id, r.graph_checkpoint_id, r.created_at, r.updated_at, "
                        "r.completed_at, d.content AS result_text, "
                        "(SELECT e.payload_json FROM local_events e WHERE e.run_id = r.id "
                        "AND e.event_type IN ('run.waiting', 'run.completed', 'run.failed', "
                        "'run.canceled', 'run.cleanup_required') "
                        "ORDER BY e.seq DESC LIMIT 1) AS result_payload_json "
                        "FROM local_runs r LEFT JOIN local_assistant_drafts d ON d.run_id = r.id "
                        "WHERE r.id = ?",
                        (root_run_id,),
                    )
                ).fetchone()
                if root_row is None:
                    raise KeyError(f"unknown run: {root_run_id}")
                if str(root_row["root_run_id"] or root_row["id"]) != root_run_id:
                    raise RunAdmissionError(
                        "collaboration_root_required",
                        "collaboration snapshots must be requested from the root Run",
                    )

                child_rows = await (
                    await conn.execute(
                        self._child_run_snapshot_sql(
                            "r.parent_run_id = ? AND r.run_kind = 'child'",
                            "ORDER BY r.created_at, r.id",
                        ),
                        (root_run_id,),
                    )
                ).fetchall()
                children = [self._child_run_projection(dict(row)) for row in child_rows]
                run_ids = [root_run_id, *(str(child["id"]) for child in children)]
                placeholders = ",".join("?" for _ in run_ids)

                cursor_rows = await (
                    await conn.execute(
                        f"SELECT run_id, COALESCE(MAX(seq), 0) AS high_watermark "
                        f"FROM local_events WHERE run_id IN ({placeholders}) GROUP BY run_id",
                        run_ids,
                    )
                ).fetchall()
                event_high_watermarks = {run_id: 0 for run_id in run_ids}
                event_high_watermarks.update(
                    {str(row["run_id"]): int(row["high_watermark"]) for row in cursor_rows}
                )

                message_rows = await (
                    await conn.execute(
                        "SELECT * FROM local_agent_messages WHERE root_run_id = ? "
                        "ORDER BY created_at, id",
                        (root_run_id,),
                    )
                ).fetchall()
                wait_rows = await (
                    await conn.execute(
                        f"SELECT * FROM local_wait_candidates WHERE run_id IN ({placeholders}) "
                        "AND status = 'pending' ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()
                resource_rows = await (
                    await conn.execute(
                        "SELECT resource_key, owner_run_id, created_at "
                        "FROM local_collaboration_resource_claims WHERE root_run_id = ? "
                        "ORDER BY resource_key, owner_run_id",
                        (root_run_id,),
                    )
                ).fetchall()
                dependency_rows = await (
                    await conn.execute(
                        f"SELECT child_run_id, dependency_run_id FROM local_child_dependencies "
                        f"WHERE child_run_id IN ({placeholders}) "
                        "ORDER BY child_run_id, dependency_run_id",
                        run_ids,
                    )
                ).fetchall()
                artifact_rows = await (
                    await conn.execute(
                        f"SELECT id, run_id, kind, title, content_type, bytes, sha256, "
                        f"storage_kind, tool_name, created_at FROM local_artifacts "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()

                root = dict(root_row)
                root_payload = _json_payload(root.pop("result_payload_json", None))
                root_result_text = root.pop("result_text", None)
                root.update(
                    result=(
                        str(root_result_text)
                        if root.get("status") == "completed" and root_result_text is not None
                        else None
                    ),
                    error=root_payload.get("error"),
                    error_type=root_payload.get("type"),
                    retryable=root_payload.get("retryable"),
                    input_tokens=int(root_payload.get("input_tokens") or 0),
                    output_tokens=int(root_payload.get("output_tokens") or 0),
                    model_calls=int(root_payload.get("model_calls") or 0),
                )
                pending_waits: list[dict[str, Any]] = []
                for row in wait_rows:
                    wait = dict(row)
                    wait["payload"] = _json_payload(wait.pop("payload_json", None))
                    decision_raw = wait.pop("decision_json", None)
                    wait["decision"] = _json_payload(decision_raw) if decision_raw else None
                    pending_waits.append(wait)
                captured_at = _now()
                await conn.commit()
                return {
                    "schema_version": 1,
                    "captured_at": captured_at,
                    "root": root,
                    "children": children,
                    "messages": [_agent_message_projection(dict(row)) for row in message_rows],
                    "pending_waits": pending_waits,
                    "resource_owners": [dict(row) for row in resource_rows],
                    "dependencies": [dict(row) for row in dependency_rows],
                    "artifacts": [dict(row) for row in artifact_rows],
                    "event_high_watermarks": event_high_watermarks,
                }
            except BaseException:
                await conn.rollback()
                raise

    @staticmethod
    async def _require_mailbox_receipt_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        tool_name: str,
        operation_id: str | None,
    ) -> None:
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != run_id:
            raise LeaseFenceError("Agent mailbox mutation requires the sender execution lease")
        execution_attempt_id = f"{lease.job_id}:{lease.lease_generation}"
        if operation_id is None:
            rows = await (
                await conn.execute(
                    "SELECT operation_id FROM local_tool_receipts WHERE run_id = ? "
                    "AND tool_name = ? AND status = 'running' AND execution_attempt_id = ?",
                    (run_id, tool_name, execution_attempt_id),
                )
            ).fetchall()
            if len(rows) != 1:
                raise ToolReceiptStateError(
                    f"Agent mailbox mutation requires one running {tool_name} receipt"
                )
            return
        receipt = await (
            await conn.execute(
                "SELECT tool_name, status, execution_attempt_id FROM local_tool_receipts "
                "WHERE operation_id = ? AND run_id = ?",
                (operation_id, run_id),
            )
        ).fetchone()
        if (
            receipt is None
            or str(receipt["tool_name"]) != tool_name
            or str(receipt["status"]) != "running"
        ):
            raise ToolReceiptStateError(
                f"Agent mailbox mutation requires its running {tool_name} receipt"
            )
        if str(receipt["execution_attempt_id"]) != execution_attempt_id:
            raise LeaseFenceError(f"{tool_name} receipt belongs to a stale execution attempt")

    async def _append_agent_message_event_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        event_type: str,
        message: dict[str, Any],
        created_at: str,
    ) -> None:
        event = await self._append_event_uncommitted(
            conn,
            run_id,
            event_type,
            payload_json=_encode_payload(_agent_message_projection(message)),
            created_at=created_at,
        )
        await self._touch_thread_for_run_event_uncommitted(
            conn,
            run_id=run_id,
            change_type=event_type,
            event_high_watermark=int(event["seq"]),
            changed_at=created_at,
        )

    async def _expire_agent_messages_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        recipient_run_id: str,
        now: str,
    ) -> None:
        expired = await (
            await conn.execute(
                "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                "AND status IN ('queued', 'delivered') AND deadline_at <= ? "
                "ORDER BY created_at, id",
                (recipient_run_id, now),
            )
        ).fetchall()
        if not expired:
            return
        await conn.execute(
            "UPDATE local_agent_messages SET status = 'expired' WHERE recipient_run_id = ? "
            "AND status IN ('queued', 'delivered') AND deadline_at <= ?",
            (recipient_run_id, now),
        )
        for row in expired:
            message = {**dict(row), "status": "expired"}
            await self._append_agent_message_event_uncommitted(
                conn,
                run_id=recipient_run_id,
                event_type="agent.message.expired",
                message=message,
                created_at=now,
            )

    async def _insert_agent_message_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: str,
        text: str,
        data_json: str,
        artifact_refs_json: str,
        ttl_seconds: int,
        correlation_id: str | None,
        in_reply_to: str | None,
        sequence: int,
        hop_count: int,
        tool_name: str,
    ) -> tuple[dict[str, Any], bool]:
        existing = await (
            await conn.execute(
                "SELECT * FROM local_agent_messages WHERE sender_operation_id = ?",
                (sender_operation_id,),
            )
        ).fetchone()
        if existing is not None:
            record = dict(existing)
            expected_correlation = correlation_id or str(record["id"])
            if any(
                (str(record[key]) if record[key] is not None else None)
                != (str(value) if value is not None else None)
                for key, value in (
                    ("sender_run_id", sender_run_id),
                    ("recipient_run_id", recipient_run_id),
                    ("kind", kind),
                    ("text", text),
                    ("data_json", data_json),
                    ("artifact_refs_json", artifact_refs_json),
                    ("ttl_seconds", ttl_seconds),
                    ("correlation_id", expected_correlation),
                    ("in_reply_to", in_reply_to),
                    ("sequence", sequence),
                    ("hop_count", hop_count),
                )
            ):
                raise CommandConflictError(
                    f"mailbox operation {sender_operation_id} was reused with a different message"
                )
            return _agent_message_projection(record), False

        await self._require_mailbox_receipt_uncommitted(
            conn,
            run_id=sender_run_id,
            tool_name=tool_name,
            operation_id=sender_operation_id,
        )
        if sender_run_id == recipient_run_id:
            raise RunAdmissionError(
                "agent_message_self_send",
                "an Agent cannot send a mailbox message to itself",
            )
        rows = await (
            await conn.execute(
                "SELECT id, principal_id, run_kind, root_run_id, parent_run_id, "
                "collaboration_depth, status FROM local_runs WHERE id IN (?, ?)",
                (sender_run_id, recipient_run_id),
            )
        ).fetchall()
        by_id = {str(row["id"]): dict(row) for row in rows}
        if sender_run_id not in by_id or recipient_run_id not in by_id:
            raise RunAdmissionError(
                "agent_message_run_not_found",
                "Agent mailbox sender or recipient does not exist",
            )
        sender = by_id[sender_run_id]
        recipient = by_id[recipient_run_id]
        sender_root = str(sender.get("root_run_id") or sender_run_id)
        recipient_root = str(recipient.get("root_run_id") or recipient_run_id)
        if sender_root != recipient_root:
            raise RunAdmissionError(
                "agent_message_foreign_root",
                "Agent mailbox participants must share one collaboration root",
            )
        if sender["principal_id"] != recipient["principal_id"]:
            raise RunAdmissionError(
                "agent_message_foreign_principal",
                "Agent mailbox participants must share one principal",
            )
        if sender["run_kind"] != "child" and recipient["run_kind"] != "child":
            raise RunAdmissionError(
                "agent_message_requires_child",
                "Agent mailbox messages require a durable child participant",
            )
        for participant in (sender, recipient):
            if participant["run_kind"] == "child" and (
                str(participant.get("parent_run_id") or "") != sender_root
                or int(participant.get("collaboration_depth") or 0) != 1
            ):
                raise RunAdmissionError(
                    "agent_message_invalid_topology",
                    "Agent mailbox participants must belong to the direct-child topology",
                )
        artifact_refs = json.loads(artifact_refs_json)
        if artifact_refs:
            placeholders = ",".join("?" for _ in artifact_refs)
            authorized_artifacts = await (
                await conn.execute(
                    "SELECT a.id FROM local_artifacts a JOIN local_runs r ON r.id = a.run_id "
                    f"WHERE a.id IN ({placeholders}) AND COALESCE(r.root_run_id, r.id) = ?",
                    (*artifact_refs, sender_root),
                )
            ).fetchall()
            authorized_ids = {str(row["id"]) for row in authorized_artifacts}
            if any(ref not in authorized_ids for ref in artifact_refs):
                raise RunAdmissionError(
                    "agent_message_artifact_forbidden",
                    "Agent mailbox artifact references must belong to the collaboration root",
                )
        if str(recipient["status"]) in _TERMINAL_RUN_STATUSES:
            raise RunAdmissionError(
                "agent_message_recipient_terminal",
                "Agent mailbox recipient is already terminal",
            )

        now = _now()
        await self._expire_agent_messages_uncommitted(
            conn,
            recipient_run_id=recipient_run_id,
            now=now,
        )
        pending = await (
            await conn.execute(
                "SELECT COUNT(*) FROM local_agent_messages WHERE recipient_run_id = ? "
                "AND status IN ('queued', 'delivered')",
                (recipient_run_id,),
            )
        ).fetchone()
        if int(pending[0]) >= MAX_AGENT_MAILBOX_PENDING:
            raise RunAdmissionError(
                "agent_message_backpressure",
                "Agent mailbox backpressure limit is reached",
            )
        root_total = await (
            await conn.execute(
                "SELECT COUNT(*) FROM local_agent_messages WHERE root_run_id = ?",
                (sender_root,),
            )
        ).fetchone()
        if int(root_total[0]) >= MAX_AGENT_MAILBOX_MESSAGES_PER_ROOT:
            raise RunAdmissionError(
                "agent_message_root_budget_exhausted",
                "Agent mailbox message budget is exhausted for this collaboration root",
            )

        message_id = _new_id("agent_message")
        record: dict[str, Any] = {
            "id": message_id,
            "root_run_id": sender_root,
            "sender_run_id": sender_run_id,
            "recipient_run_id": recipient_run_id,
            "sender_operation_id": sender_operation_id,
            "kind": kind,
            "text": text,
            "data_json": data_json,
            "artifact_refs_json": artifact_refs_json,
            "correlation_id": correlation_id or message_id,
            "in_reply_to": in_reply_to,
            "sequence": sequence,
            "hop_count": hop_count,
            "status": "queued",
            "ttl_seconds": ttl_seconds,
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
            "created_at": now,
            "delivered_at": None,
            "acknowledged_at": None,
        }
        await conn.execute(
            "INSERT INTO local_agent_messages "
            "(id, root_run_id, sender_run_id, recipient_run_id, sender_operation_id, kind, "
            "text, data_json, artifact_refs_json, correlation_id, in_reply_to, sequence, "
            "hop_count, status, ttl_seconds, deadline_at, created_at, delivered_at, "
            "acknowledged_at) VALUES (:id, :root_run_id, :sender_run_id, :recipient_run_id, "
            ":sender_operation_id, :kind, :text, :data_json, :artifact_refs_json, "
            ":correlation_id, :in_reply_to, :sequence, :hop_count, :status, :ttl_seconds, "
            ":deadline_at, :created_at, :delivered_at, :acknowledged_at)",
            record,
        )
        await self._append_agent_message_event_uncommitted(
            conn,
            run_id=sender_run_id,
            event_type="agent.message.sent",
            message=record,
            created_at=now,
        )
        return _agent_message_projection(record), True

    async def send_agent_message(
        self,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: str,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        normalized = _normalize_agent_message_content(
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        async with self.run_write_transaction(sender_run_id) as conn:
            return await self._insert_agent_message_uncommitted(
                conn,
                sender_run_id=sender_run_id,
                sender_operation_id=sender_operation_id,
                recipient_run_id=recipient_run_id,
                kind=normalized[0],
                text=normalized[1],
                data_json=normalized[2],
                artifact_refs_json=normalized[3],
                ttl_seconds=normalized[4],
                correlation_id=None,
                in_reply_to=None,
                sequence=1,
                hop_count=0,
                tool_name="mailbox.send",
            )

    async def reply_agent_message(
        self,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        in_reply_to: str,
        kind: str,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        normalized = _normalize_agent_message_content(
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        async with self.run_write_transaction(sender_run_id) as conn:
            replay = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE sender_operation_id = ?",
                    (sender_operation_id,),
                )
            ).fetchone()
            original = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE id = ?",
                    (in_reply_to,),
                )
            ).fetchone()
            if original is None:
                raise RunAdmissionError(
                    "agent_message_not_found",
                    "Agent mailbox reply target does not exist",
                )
            original_record = dict(original)
            if str(original_record["recipient_run_id"]) != sender_run_id:
                raise RunAdmissionError(
                    "agent_message_reply_forbidden",
                    "an Agent can only reply to a message addressed to it",
                )
            if str(original_record["kind"]) not in {"request", "question", "update"}:
                raise RunAdmissionError(
                    "agent_message_reply_terminal",
                    "result and cancel messages cannot be replied to",
                )
            if replay is None:
                thread_head = await (
                    await conn.execute(
                        "SELECT MAX(sequence), MAX(hop_count) FROM local_agent_messages "
                        "WHERE correlation_id = ?",
                        (original_record["correlation_id"],),
                    )
                ).fetchone()
                next_sequence = int(thread_head[0] or 0) + 1
                next_hop = int(thread_head[1] or 0) + 1
            else:
                next_sequence = int(replay["sequence"])
                next_hop = int(replay["hop_count"])
            if next_hop > MAX_AGENT_MAILBOX_HOPS:
                raise RunAdmissionError(
                    "agent_message_hop_limit",
                    "Agent mailbox conversation hop limit is exhausted",
                )
            return await self._insert_agent_message_uncommitted(
                conn,
                sender_run_id=sender_run_id,
                sender_operation_id=sender_operation_id,
                recipient_run_id=str(original_record["sender_run_id"]),
                kind=normalized[0],
                text=normalized[1],
                data_json=normalized[2],
                artifact_refs_json=normalized[3],
                ttl_seconds=normalized[4],
                correlation_id=str(original_record["correlation_id"]),
                in_reply_to=in_reply_to,
                sequence=next_sequence,
                hop_count=next_hop,
                tool_name="mailbox.reply",
            )

    async def deliver_agent_messages(self, recipient_run_id: str) -> list[dict[str, Any]]:
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != recipient_run_id:
            raise LeaseFenceError("Agent mailbox delivery requires the recipient execution lease")
        async with self.run_write_transaction(recipient_run_id) as conn:
            now = _now()
            await self._expire_agent_messages_uncommitted(
                conn,
                recipient_run_id=recipient_run_id,
                now=now,
            )
            queued = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                    "AND status = 'queued' ORDER BY created_at, id",
                    (recipient_run_id,),
                )
            ).fetchall()
            for row in queued:
                message = {**dict(row), "status": "delivered", "delivered_at": now}
                await conn.execute(
                    "UPDATE local_agent_messages SET status = 'delivered', delivered_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (now, row["id"]),
                )
                await self._append_agent_message_event_uncommitted(
                    conn,
                    run_id=recipient_run_id,
                    event_type="agent.message.received",
                    message=message,
                    created_at=now,
                )
            rows = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                    "AND status = 'delivered' ORDER BY created_at, id",
                    (recipient_run_id,),
                )
            ).fetchall()
            return [_agent_message_projection(dict(row)) for row in rows]

    async def ack_agent_messages(
        self,
        *,
        recipient_run_id: str,
        message_ids: Sequence[str],
        operation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(message_id) for message_id in message_ids))
        if not normalized_ids or len(normalized_ids) > MAX_AGENT_MAILBOX_PENDING:
            raise ValueError("Agent mailbox acknowledgement message_ids are invalid")
        async with self.run_write_transaction(recipient_run_id) as conn:
            await self._require_mailbox_receipt_uncommitted(
                conn,
                run_id=recipient_run_id,
                tool_name="mailbox.ack",
                operation_id=operation_id,
            )
            placeholders = ",".join("?" for _ in normalized_ids)
            rows = await (
                await conn.execute(
                    f"SELECT * FROM local_agent_messages WHERE id IN ({placeholders})",
                    normalized_ids,
                )
            ).fetchall()
            by_id = {str(row["id"]): dict(row) for row in rows}
            if any(message_id not in by_id for message_id in normalized_ids):
                raise RunAdmissionError(
                    "agent_message_not_found",
                    "Agent mailbox acknowledgement target does not exist",
                )
            now = _now()
            for message_id in normalized_ids:
                record = by_id[message_id]
                if str(record["recipient_run_id"]) != recipient_run_id:
                    raise RunAdmissionError(
                        "agent_message_ack_forbidden",
                        "an Agent can only acknowledge a message addressed to it",
                    )
                if str(record["status"]) == "acknowledged":
                    continue
                if str(record["status"]) != "delivered":
                    raise RunAdmissionError(
                        "agent_message_not_delivered",
                        "Agent mailbox message must be delivered before acknowledgement",
                    )
                await conn.execute(
                    "UPDATE local_agent_messages SET status = 'acknowledged', "
                    "acknowledged_at = ? WHERE id = ? AND status = 'delivered'",
                    (now, message_id),
                )
                record.update(status="acknowledged", acknowledged_at=now)
                await self._append_agent_message_event_uncommitted(
                    conn,
                    run_id=recipient_run_id,
                    event_type="agent.message.acknowledged",
                    message=record,
                    created_at=now,
                )
            return [_agent_message_projection(by_id[message_id]) for message_id in normalized_ids]

    async def list_agent_inbox(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                "ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [_agent_message_projection(dict(row)) for row in rows]

    async def list_agent_outbox(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_agent_messages WHERE sender_run_id = ? "
                "ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [_agent_message_projection(dict(row)) for row in rows]

    async def get_assistant_draft(self, run_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_assistant_drafts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def get_runtime_settings(self) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT settings_json, version, updated_at FROM local_runtime_settings WHERE id = 1"
            )
        ).fetchone()
        if row is None:
            return None
        return {
            "settings": _json_payload(row["settings_json"]),
            "version": int(row["version"]),
            "updated_at": str(row["updated_at"]),
        }

    async def patch_runtime_settings(
        self,
        patch: dict[str, Any],
        *,
        initial_settings: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        initial_payload = {**initial_settings, **patch}
        patch_json = _encode_payload(patch)
        cursor = await self._conn.execute(
            "INSERT INTO local_runtime_settings (id, settings_json, version, updated_at) "
            "VALUES (1, ?, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "settings_json = json_patch(local_runtime_settings.settings_json, ?), "
            "version = local_runtime_settings.version + 1, "
            "updated_at = excluded.updated_at "
            "WHERE json_patch(local_runtime_settings.settings_json, ?) "
            "IS NOT local_runtime_settings.settings_json "
            "RETURNING settings_json, version, updated_at",
            (
                _encode_payload(initial_payload),
                now,
                patch_json,
                patch_json,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            current = await self.get_runtime_settings()
            assert current is not None
            return current
        assert row is not None
        return {
            "settings": _json_payload(row["settings_json"]),
            "version": int(row["version"]),
            "updated_at": str(row["updated_at"]),
        }

    async def get_mcp_catalog(self, server_name: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_mcp_catalog WHERE server_name = ?",
                (server_name,),
            )
        ).fetchone()
        return _decode_mcp_catalog_row(row)

    async def list_mcp_catalogs(self) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute("SELECT * FROM local_mcp_catalog ORDER BY server_name")
        ).fetchall()
        return [record for row in rows if (record := _decode_mcp_catalog_row(row))]

    async def upsert_mcp_catalog(
        self,
        *,
        server_name: str,
        config_fingerprint: str,
        tools: list[dict[str, Any]],
        status: str,
        error_type: str | None,
    ) -> dict[str, Any]:
        if status not in {"ready", "error", "stale"}:
            raise ValueError("invalid MCP catalog status")
        now = _now()
        cursor = await self._conn.execute(
            "INSERT INTO local_mcp_catalog "
            "(server_name, config_fingerprint, tools_json, status, error_type, "
            "version, updated_at, last_success_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(server_name) DO UPDATE SET "
            "config_fingerprint = excluded.config_fingerprint, "
            "tools_json = excluded.tools_json, status = excluded.status, "
            "error_type = excluded.error_type, version = local_mcp_catalog.version + 1, "
            "updated_at = excluded.updated_at, "
            "last_success_at = COALESCE(excluded.last_success_at, local_mcp_catalog.last_success_at) "
            "RETURNING *",
            (
                server_name,
                config_fingerprint,
                json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                status,
                error_type,
                now,
                now if status == "ready" else None,
            ),
        )
        row = await cursor.fetchone()
        record = _decode_mcp_catalog_row(row)
        assert record is not None
        return record

    async def delete_mcp_catalog(self, server_name: str) -> None:
        await self._conn.execute(
            "DELETE FROM local_mcp_catalog WHERE server_name = ?",
            (server_name,),
        )

    async def list_model_connections(self, *, principal_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM model_connections WHERE principal_id = ? ORDER BY created_at, id",
                (principal_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM model_connections WHERE principal_id = ? AND id = ?",
                (principal_id, connection_id),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def create_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
        preset_id: str,
        name: str,
        region: str,
        adapter_id: str,
        base_url: str,
        requires_api_key: bool,
        credential_ref: str,
        models: list[dict[str, Any]],
        catalog_status: str,
    ) -> dict[str, Any]:
        now = _now()
        models_json = json.dumps(
            models,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        await self._conn.execute(
            "INSERT INTO model_connections "
            "(principal_id, id, preset_id, name, region, adapter_id, base_url, "
            " requires_api_key, credential_ref, models_json, catalog_status, version, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                principal_id,
                connection_id,
                preset_id,
                name,
                region,
                adapter_id,
                base_url,
                int(requires_api_key),
                credential_ref,
                models_json,
                catalog_status,
                now,
                now,
            ),
        )
        connection = await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        assert connection is not None
        return connection

    async def delete_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> dict[str, Any] | None:
        connection = await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if connection is None:
            return None
        await self._conn.execute(
            "DELETE FROM model_connections WHERE principal_id = ? AND id = ?",
            (principal_id, connection_id),
        )
        return connection

    async def update_model_connection_catalog(
        self,
        *,
        principal_id: str,
        connection_id: str,
        models: list[dict[str, Any]],
        catalog_status: str,
    ) -> dict[str, Any] | None:
        connection = await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if connection is None:
            return None
        models_json = json.dumps(
            models,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            connection["models_json"] == models_json
            and connection["catalog_status"] == catalog_status
        ):
            return connection
        cursor = await self._conn.execute(
            "UPDATE model_connections SET models_json = ?, catalog_status = ?, "
            "updated_at = ? "
            "WHERE principal_id = ? AND id = ?",
            (
                models_json,
                catalog_status,
                _now(),
                principal_id,
                connection_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )

    async def replace_model_connection_credential(
        self,
        *,
        principal_id: str,
        connection_id: str,
        credential_ref: str,
        base_url: str,
        models: list[dict[str, Any]],
        catalog_status: str,
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "UPDATE model_connections SET credential_ref = ?, base_url = ?, models_json = ?, "
            "catalog_status = ?, version = version + 1, updated_at = ? "
            "WHERE principal_id = ? AND id = ?",
            (
                credential_ref,
                base_url,
                json.dumps(
                    models,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                catalog_status,
                _now(),
                principal_id,
                connection_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )

    async def replace_official_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
        name: str,
        base_url: str,
        credential_ref: str,
        models: list[dict[str, Any]],
        catalog_status: str,
        capability_bindings: dict[str, dict[str, str]],
        timestamp: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Atomically install one official connection and retire every older one."""
        models_json = json.dumps(
            models,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        async def replace() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            async with aiosqlite.connect(str(self._db_path)) as conn:
                await _configure_connection(conn)
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    previous_rows = await (
                        await conn.execute(
                            "SELECT * FROM model_connections "
                            "WHERE principal_id = ? AND preset_id = 'shejane-official'",
                            (principal_id,),
                        )
                    ).fetchall()
                    previous_connections = [dict(row) for row in previous_rows]
                    previous_ids = {str(row["id"]) for row in previous_connections}
                    await conn.execute(
                        "INSERT INTO model_connections "
                        "(principal_id, id, preset_id, name, region, adapter_id, base_url, "
                        "requires_api_key, credential_ref, models_json, catalog_status, version, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, 'official', 'openai_chat', "
                        "?, 1, ?, ?, ?, 1, ?, ?)",
                        (
                            principal_id,
                            connection_id,
                            "shejane-official",
                            name,
                            base_url,
                            credential_ref,
                            models_json,
                            catalog_status,
                            timestamp,
                            timestamp,
                        ),
                    )
                    for capability, binding in capability_bindings.items():
                        current = await (
                            await conn.execute(
                                "SELECT connection_id FROM model_capability_bindings "
                                "WHERE principal_id = ? AND capability = ?",
                                (principal_id, capability),
                            )
                        ).fetchone()
                        if (
                            current is not None
                            and str(current["connection_id"]) not in previous_ids
                        ):
                            continue
                        await conn.execute(
                            "INSERT INTO model_capability_bindings "
                            "(principal_id, capability, connection_id, connection_version, model_id, "
                            "protocol, revision, updated_at) VALUES (?, ?, ?, 1, ?, ?, 1, ?) "
                            "ON CONFLICT(principal_id, capability) DO UPDATE SET "
                            "connection_id = excluded.connection_id, "
                            "connection_version = excluded.connection_version, "
                            "model_id = excluded.model_id, protocol = excluded.protocol, "
                            "revision = model_capability_bindings.revision + 1, "
                            "updated_at = excluded.updated_at",
                            (
                                principal_id,
                                capability,
                                connection_id,
                                binding["model_id"],
                                binding["protocol"],
                                timestamp,
                            ),
                        )
                    if previous_ids:
                        await conn.executemany(
                            "DELETE FROM model_connections WHERE principal_id = ? AND id = ?",
                            [(principal_id, previous_id) for previous_id in previous_ids],
                        )
                    await conn.commit()
                except BaseException:
                    if conn.in_transaction:
                        await conn.rollback()
                    raise
            connection = await self.get_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            assert connection is not None
            return connection, previous_connections

        return await _finish_task_despite_cancellation(asyncio.create_task(replace()))

    async def list_model_capability_bindings(
        self,
        *,
        principal_id: str,
    ) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM model_capability_bindings "
                "WHERE principal_id = ? ORDER BY capability",
                (principal_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_model_capability_binding(
        self,
        *,
        principal_id: str,
        capability: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM model_capability_bindings WHERE principal_id = ? AND capability = ?",
                (principal_id, capability),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def set_model_capability_binding(
        self,
        *,
        principal_id: str,
        capability: str,
        connection_id: str,
        connection_version: int,
        model_id: str,
        protocol: str,
    ) -> dict[str, Any]:
        cursor = await self._conn.execute(
            "INSERT INTO model_capability_bindings "
            "(principal_id, capability, connection_id, connection_version, model_id, "
            "protocol, revision, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(principal_id, capability) DO UPDATE SET "
            "connection_id = excluded.connection_id, "
            "connection_version = excluded.connection_version, "
            "model_id = excluded.model_id, protocol = excluded.protocol, "
            "revision = model_capability_bindings.revision + 1, "
            "updated_at = excluded.updated_at RETURNING *",
            (
                principal_id,
                capability,
                connection_id,
                connection_version,
                model_id,
                protocol,
                _now(),
            ),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def create_model_capability_binding_if_absent(
        self,
        *,
        principal_id: str,
        capability: str,
        connection_id: str,
        connection_version: int,
        model_id: str,
        protocol: str,
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "INSERT INTO model_capability_bindings "
            "(principal_id, capability, connection_id, connection_version, model_id, "
            "protocol, revision, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(principal_id, capability) DO NOTHING RETURNING *",
            (
                principal_id,
                capability,
                connection_id,
                connection_version,
                model_id,
                protocol,
                _now(),
            ),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def delete_model_capability_binding(
        self,
        *,
        principal_id: str,
        capability: str,
    ) -> dict[str, Any] | None:
        existing = await self.get_model_capability_binding(
            principal_id=principal_id,
            capability=capability,
        )
        if existing is None:
            return None
        await self._conn.execute(
            "DELETE FROM model_capability_bindings WHERE principal_id = ? AND capability = ?",
            (principal_id, capability),
        )
        return existing

    # --- workspaces ---

    async def create_workspace(self, *, principal_id: str, path: str, label: str) -> dict[str, Any]:
        ws = {
            "id": _new_id("ws"),
            "principal_id": principal_id,
            "path": path,
            "label": label,
            "created_at": _now(),
            "last_used_at": _now(),
        }
        await self._conn.execute(
            "INSERT INTO local_workspaces "
            "(id, principal_id, path, label, created_at, last_used_at) "
            "VALUES (:id, :principal_id, :path, :label, :created_at, :last_used_at) "
            "ON CONFLICT(principal_id, path) DO NOTHING",
            ws,
        )
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_workspaces WHERE principal_id = ? AND path = ?",
                (principal_id, path),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def list_workspaces(self, *, principal_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_workspaces WHERE principal_id = ? ORDER BY last_used_at DESC",
            (principal_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def workspace_by_path(self, *, principal_id: str, path: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_workspaces WHERE principal_id = ? AND path = ?",
            (principal_id, path),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_outcome_unknown_tool_receipt_in_lineage(
        self,
        *,
        current_run_id: str,
        tool_name: str,
        arguments_hash: str,
        risk: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "WITH RECURSIVE lineage(id, owner, depth) AS ("
                "SELECT parent_run_id, principal_id, 0 FROM local_runs "
                "WHERE id = ? AND parent_run_id IS NOT NULL UNION ALL "
                "SELECT ancestor.parent_run_id, lineage.owner, lineage.depth + 1 "
                "FROM local_runs AS ancestor JOIN lineage ON ancestor.id = lineage.id "
                "WHERE ancestor.principal_id = lineage.owner "
                "AND ancestor.parent_run_id IS NOT NULL AND lineage.depth < 64"
                ") SELECT local_tool_receipts.* FROM local_tool_receipts "
                "JOIN lineage ON lineage.id = local_tool_receipts.run_id "
                "JOIN local_runs AS ancestor ON ancestor.id = lineage.id "
                "AND ancestor.principal_id = lineage.owner "
                "WHERE local_tool_receipts.tool_name = ? "
                "AND local_tool_receipts.arguments_hash = ? "
                "AND local_tool_receipts.risk = ? "
                "AND local_tool_receipts.status = 'outcome_unknown' "
                "ORDER BY lineage.depth, local_tool_receipts.updated_at DESC LIMIT 1",
                (current_run_id, tool_name, arguments_hash, risk),
            )
        ).fetchone()
        return dict(row) if row else None

    async def workspace_admission_error(self, *, principal_id: str, path: str | None) -> str | None:
        owner_error = await self._workspace_owner_error(
            self._conn,
            principal_id=principal_id,
            path=path,
        )
        return owner_error or await self._workspace_path_error(path)

    @staticmethod
    async def _workspace_owner_error(
        conn: aiosqlite.Connection, *, principal_id: str, path: str | None
    ) -> str | None:
        if path is None:
            return None
        workspace = await (
            await conn.execute(
                "SELECT 1 FROM local_workspaces WHERE principal_id = ? AND path = ?",
                (principal_id, path),
            )
        ).fetchone()
        if workspace is None:
            return "workspace is not authorized"
        return None

    @staticmethod
    async def _workspace_path_error(path: str | None) -> str | None:
        if path is None:
            return None
        return await asyncio.to_thread(LocalStore._workspace_path_error_sync, path)

    @staticmethod
    def _workspace_path_error_sync(path: str) -> str | None:
        root = Path(path)
        try:
            if root.is_symlink() or str(root.resolve(strict=True)) != path or not root.is_dir():
                return "workspace is no longer available"
        except OSError:
            return "workspace is no longer available"
        return None

    async def delete_workspace(self, *, principal_id: str, workspace_id: str) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM local_workspaces WHERE principal_id = ? AND id = ?",
            (principal_id, workspace_id),
        )
        return cursor.rowcount > 0

    async def touch_workspace(self, *, principal_id: str, workspace_id: str) -> None:
        await self._conn.execute(
            "UPDATE local_workspaces SET last_used_at = ? WHERE principal_id = ? AND id = ?",
            (_now(), principal_id, workspace_id),
        )

    # --- runs ---

    async def create_run(
        self,
        *,
        principal_id: str,
        goal: str,
        workspace_path: str | None,
        parent_run_id: str | None = None,
        settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        mode: str = "fast",
        graph_thread_id: str | None = None,
        graph_checkpoint_id: str | None = None,
        graph_definition_id: str | None = None,
        graph_input_kind: str = "new",
        run_kind: str | None = None,
        root_run_id: str | None = None,
        agent_definition_id: str = "shejane.default",
        agent_definition_version: str = "1",
        collaboration_depth: int = 0,
        collaboration_policy: dict[str, Any] | None = None,
        spawn_operation_id: str | None = None,
    ) -> dict[str, Any]:
        if parent_run_id is not None and root_run_id is None:
            parent = await (
                await self._conn.execute(
                    "SELECT root_run_id FROM local_runs WHERE principal_id = ? AND id = ?",
                    (principal_id, parent_run_id),
                )
            ).fetchone()
            if parent is not None:
                root_run_id = str(parent["root_run_id"] or parent_run_id)
        run = self._new_run_record(
            principal_id=principal_id,
            goal=goal,
            workspace_path=workspace_path,
            parent_run_id=parent_run_id,
            settings=settings,
            metadata=metadata,
            mode=mode,
            graph_thread_id=graph_thread_id,
            graph_checkpoint_id=graph_checkpoint_id,
            graph_definition_id=graph_definition_id,
            graph_input_kind=graph_input_kind,
            run_kind=run_kind,
            root_run_id=root_run_id,
            agent_definition_id=agent_definition_id,
            agent_definition_version=agent_definition_version,
            collaboration_depth=collaboration_depth,
            collaboration_policy=collaboration_policy,
            spawn_operation_id=spawn_operation_id,
        )
        await self._insert_run(self._conn, run)
        return run

    @staticmethod
    def _new_run_record(
        *,
        principal_id: str,
        goal: str,
        workspace_path: str | None,
        parent_run_id: str | None,
        settings: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
        mode: str,
        history: list[dict[str, str]] | None = None,
        graph_thread_id: str | None = None,
        graph_checkpoint_id: str | None = None,
        graph_definition_id: str | None = None,
        graph_input_kind: str = "new",
        thread_id: str | None = None,
        assistant_item_id: str | None = None,
        user_input: str | None = None,
        run_kind: str | None = None,
        root_run_id: str | None = None,
        agent_definition_id: str = "shejane.default",
        agent_definition_version: str = "1",
        collaboration_depth: int = 0,
        collaboration_policy: dict[str, Any] | None = None,
        spawn_operation_id: str | None = None,
    ) -> dict[str, Any]:
        if graph_input_kind not in {"new", "fork"}:
            raise ValueError(f"invalid graph input kind: {graph_input_kind}")
        effective_run_kind = run_kind or ("fork" if graph_input_kind == "fork" else "turn")
        if effective_run_kind not in {"turn", "fork", "child"}:
            raise ValueError(f"invalid run kind: {effective_run_kind}")
        if collaboration_depth < 0:
            raise ValueError("collaboration depth must be non-negative")
        run_id = _new_id("run")
        return {
            "id": run_id,
            "principal_id": principal_id,
            "run_kind": effective_run_kind,
            "root_run_id": root_run_id or run_id,
            "agent_definition_id": agent_definition_id,
            "agent_definition_version": agent_definition_version,
            "collaboration_depth": collaboration_depth,
            "collaboration_policy_json": json.dumps(
                collaboration_policy or {}, ensure_ascii=False, sort_keys=True
            ),
            "spawn_operation_id": spawn_operation_id,
            "graph_thread_id": graph_thread_id or _new_id("thread"),
            "graph_checkpoint_id": graph_checkpoint_id,
            "graph_definition_id": graph_definition_id,
            "graph_input_kind": graph_input_kind,
            "thread_id": thread_id,
            "assistant_item_id": assistant_item_id,
            "user_input": user_input or goal,
            "goal": goal,
            "workspace_path": workspace_path,
            "status": "queued",
            "history_json": json.dumps(history or [], ensure_ascii=False, default=str),
            "parent_run_id": parent_run_id,
            "settings_json": json.dumps(settings or {}, ensure_ascii=False),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, default=str),
            "mode": mode,
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
        }

    @staticmethod
    async def _insert_run(conn: aiosqlite.Connection, run: dict[str, Any]) -> None:
        await conn.execute(
            "INSERT INTO local_runs "
            "(id, principal_id, run_kind, root_run_id, agent_definition_id, "
            " agent_definition_version, collaboration_depth, collaboration_policy_json, "
            " spawn_operation_id, graph_thread_id, graph_checkpoint_id, graph_definition_id, "
            " graph_input_kind, thread_id, assistant_item_id, user_input, goal, workspace_path, status, history_json, parent_run_id, "
            " settings_json, metadata_json, mode, created_at, updated_at, completed_at) "
            "VALUES (:id, :principal_id, :run_kind, :root_run_id, :agent_definition_id, "
            "        :agent_definition_version, :collaboration_depth, "
            "        :collaboration_policy_json, :spawn_operation_id, "
            "        :graph_thread_id, :graph_checkpoint_id, "
            "        :graph_definition_id, :graph_input_kind, :thread_id, :assistant_item_id, :user_input, :goal, :workspace_path, "
            "        :status, :history_json, :parent_run_id, :settings_json, :metadata_json, "
            "        :mode, :created_at, :updated_at, :completed_at)",
            run,
        )

    @staticmethod
    def _run_job_input(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "principal_id": run["principal_id"],
            "goal": run["goal"],
            "user_input": run.get("user_input") or run["goal"],
            "workspace_path": run["workspace_path"],
            "mode": run["mode"],
            "history": json.loads(run["history_json"] or "[]"),
            "settings": json.loads(run["settings_json"] or "{}"),
            "metadata": json.loads(run["metadata_json"] or "{}"),
            "run_kind": run.get("run_kind") or "turn",
            "root_run_id": run.get("root_run_id") or run.get("id") or run["run_id"],
            "agent_definition_id": run.get("agent_definition_id") or "shejane.default",
            "agent_definition_version": run.get("agent_definition_version") or "1",
            "collaboration_depth": int(run.get("collaboration_depth") or 0),
            "collaboration_policy": json.loads(run.get("collaboration_policy_json") or "{}"),
        }

    @staticmethod
    def _new_run_job_record(
        *,
        run_id: str,
        kind: str,
        input_payload: dict[str, Any],
        resume_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        created_at = _now()
        return {
            "id": _new_id("job"),
            "run_id": run_id,
            "kind": kind,
            "status": "pending",
            "input_json": _encode_payload(input_payload),
            "resume_json": _encode_payload(resume_payload) if resume_payload is not None else None,
            "lease_owner": None,
            "lease_generation": 0,
            "lease_expires_at": None,
            "attempt": 0,
            "cancel_requested_at": None,
            "created_at": created_at,
            "updated_at": created_at,
            "finished_at": None,
        }

    @staticmethod
    async def _insert_run_job(conn: aiosqlite.Connection, job: dict[str, Any]) -> None:
        await conn.execute(
            "INSERT INTO local_run_jobs "
            "(id, run_id, kind, status, input_json, resume_json, lease_owner, "
            " lease_generation, lease_expires_at, attempt, cancel_requested_at, "
            " created_at, updated_at, finished_at) "
            "VALUES (:id, :run_id, :kind, :status, :input_json, :resume_json, "
            " :lease_owner, :lease_generation, :lease_expires_at, :attempt, "
            " :cancel_requested_at, :created_at, :updated_at, :finished_at)",
            job,
        )

    @staticmethod
    async def _accepted_run_for_command(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        command_id: str,
        payload_json: str,
    ) -> dict[str, Any] | None:
        command = await (
            await conn.execute(
                "SELECT payload_json, run_id FROM local_commands WHERE principal_id = ? AND id = ?",
                (principal_id, command_id),
            )
        ).fetchone()
        if command is None:
            return None
        if command["payload_json"] != payload_json:
            raise CommandConflictError(
                f"command {command_id} was already accepted with different content"
            )
        run = await (
            await conn.execute(
                "SELECT r.*, c.id AS command_id, c.client_message_id "
                "FROM local_runs r JOIN local_commands c ON c.run_id = r.id "
                "AND c.principal_id = r.principal_id "
                "WHERE r.id = ? AND c.principal_id = ? AND c.id = ?",
                (command["run_id"], principal_id, command_id),
            )
        ).fetchone()
        if run is None:
            raise RuntimeError(f"command {command_id} references a missing run")
        return dict(run)

    @staticmethod
    async def _accepted_command_receipt_uncommitted(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload_json: str,
    ) -> dict[str, Any] | None:
        existing = await (
            await conn.execute(
                "SELECT command_type, payload_json, response_json "
                "FROM local_commands WHERE principal_id = ? AND id = ?",
                (principal_id, command_id),
            )
        ).fetchone()
        if existing is None:
            return None
        if existing["command_type"] != command_type or existing["payload_json"] != payload_json:
            raise CommandConflictError(
                f"command {command_id} was already accepted with different content"
            )
        return json.loads(existing["response_json"])

    async def accepted_command_receipt(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._accepted_command_receipt_uncommitted(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            command_type=command_type,
            payload_json=_encode_payload(payload),
        )

    async def record_command_receipt(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload_json = _encode_payload(payload)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        now,
                    ),
                )
                await conn.commit()
                return receipt
            except BaseException:
                await conn.rollback()
                raise

    async def install_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_payload: dict[str, Any],
        manifest: dict[str, Any],
        digest: str,
        signature_status: str,
        signer_key_id: str | None,
        compatibility: str,
        source: str,
        command_type: str = "plugin.install",
        receipt_type: str = "plugin.install",
        receipt_extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(command_payload)
        plugin_id = str(manifest["id"])
        version = str(manifest["version"])
        execution_kind = str(manifest["runtime"]["execution"]["kind"])
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                bound = await (
                    await conn.execute(
                        "SELECT digest FROM plugin_versions WHERE plugin_id = ? AND version = ?",
                        (plugin_id, version),
                    )
                ).fetchone()
                if bound is not None and bound["digest"] != digest:
                    raise PluginVersionConflictError(
                        f"plugin {plugin_id} version {version} already has different content"
                    )
                by_digest = await (
                    await conn.execute(
                        "SELECT plugin_id, version FROM plugin_versions WHERE digest = ?",
                        (digest,),
                    )
                ).fetchone()
                if by_digest is not None and (
                    by_digest["plugin_id"] != plugin_id or by_digest["version"] != version
                ):
                    raise PluginVersionConflictError("plugin digest is bound to another identity")
                if bound is None:
                    await conn.execute(
                        "INSERT INTO plugin_versions "
                        "(plugin_id, version, digest, manifest_json, execution_kind, "
                        "signature_status, signer_key_id, compatibility, source, state, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'installed', ?, ?)",
                        (
                            plugin_id,
                            version,
                            digest,
                            _encode_payload(manifest),
                            execution_kind,
                            signature_status,
                            signer_key_id,
                            compatibility,
                            source,
                            now,
                            now,
                        ),
                    )

                installation = await (
                    await conn.execute(
                        "SELECT active_digest, enabled, retired_at FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is not None and installation["active_digest"] != digest:
                    raise PluginVersionConflictError(
                        f"plugin {plugin_id} is already installed; use plugin.update"
                    )
                if installation is None:
                    await conn.execute(
                        "INSERT INTO plugin_installations "
                        "(principal_id, plugin_id, active_digest, enabled, source, created_at, updated_at) "
                        "VALUES (?, ?, ?, 0, ?, ?, ?)",
                        (principal_id, plugin_id, digest, source, now, now),
                    )
                    enabled = False
                else:
                    enabled = bool(installation["enabled"])
                    if installation["retired_at"] is not None:
                        await conn.execute(
                            "UPDATE plugin_installations SET retired_at = NULL, "
                            "revision = revision + 1, updated_at = ? "
                            "WHERE principal_id = ? AND plugin_id = ?",
                            (now, principal_id, plugin_id),
                        )
                        await conn.execute(
                            "UPDATE plugin_versions SET state = 'installed', retired_at = NULL, "
                            "updated_at = ? WHERE digest = ?",
                            (now, digest),
                        )

                receipt = {
                    "type": receipt_type,
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "version": version,
                    "digest": digest,
                    "installed": True,
                    "enabled": enabled,
                }
                if receipt_extra:
                    receipt.update(receipt_extra)
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def list_plugins(self, *, principal_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT v.*, i.enabled, i.model_binding_json, i.model_binding_revision, "
                "i.retired_at AS installation_retired_at "
                "FROM plugin_installations i "
                "JOIN plugin_versions v ON v.digest = i.active_digest "
                "WHERE i.principal_id = ? ORDER BY v.plugin_id",
                (principal_id,),
            )
        ).fetchall()
        return [
            {
                **dict(row),
                "manifest": json.loads(row["manifest_json"]),
                "enabled": bool(row["enabled"]),
                "model_binding": (
                    json.loads(row["model_binding_json"])
                    if row["model_binding_json"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    async def referenced_runtime_asset_digests(self) -> set[str]:
        rows = await (
            await self._conn.execute(
                "SELECT DISTINCT v.manifest_json FROM plugin_versions v "
                "WHERE v.digest IN ("
                "SELECT active_digest FROM plugin_installations WHERE retired_at IS NULL "
                "UNION SELECT digest FROM run_plugin_bindings)"
            )
        ).fetchall()
        digests: set[str] = set()
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            execution = manifest.get("runtime", {}).get("execution", {})
            for reference in execution.get("runtime_assets", []):
                digest = reference.get("digest")
                if isinstance(digest, str):
                    digests.add(digest)
        return digests

    async def discard_stale_fixed_capability(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> bool:
        """Disable a `runtime_builtin` plugin installation whose source
        package is no longer available.  Returns `True` when a change
        was committed.  User-installed plugins are never touched."""
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT enabled, source FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if row is None or not bool(row["enabled"]):
                    await conn.rollback()
                    return False
                if row["source"] != "runtime_builtin":
                    await conn.rollback()
                    return False
                await conn.execute(
                    "UPDATE plugin_installations SET enabled = 0, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (_now(), principal_id, plugin_id),
                )
                await conn.commit()
                return True
            except BaseException:
                await conn.rollback()
                raise

    async def get_plugin_setup_flow(self, *, principal_id: str, plugin_id: str) -> dict[str, Any]:
        row = await (
            await self._conn.execute(
                "SELECT stage, revision, updated_at FROM plugin_setup_flows "
                "WHERE principal_id = ? AND plugin_id = ?",
                (principal_id, plugin_id),
            )
        ).fetchone()
        if row is None:
            return {"stage": "idle", "revision": 0, "updated_at": None}
        return dict(row)

    async def begin_plugin_setup_action(
        self,
        *,
        principal_id: str,
        plugin_id: str,
        expected_revision: int,
        next_stage: str,
    ) -> dict[str, Any]:
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT stage, revision FROM plugin_setup_flows "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                revision = int(row["revision"]) if row is not None else 0
                if revision != expected_revision:
                    raise PluginStateError("plugin_setup_stale", "plugin setup state changed")
                next_revision = revision + 1
                await conn.execute(
                    "INSERT INTO plugin_setup_flows "
                    "(principal_id, plugin_id, stage, revision, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(principal_id, plugin_id) DO UPDATE SET "
                    "stage = excluded.stage, revision = excluded.revision, "
                    "updated_at = excluded.updated_at",
                    (principal_id, plugin_id, next_stage, next_revision, now),
                )
                await conn.commit()
                return {
                    "stage": next_stage,
                    "revision": next_revision,
                    "updated_at": now,
                }
            except BaseException:
                await conn.rollback()
                raise

    async def get_plugin(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT v.*, i.enabled, i.model_binding_json, i.model_binding_revision, "
                "i.retired_at AS installation_retired_at "
                "FROM plugin_installations i "
                "JOIN plugin_versions v ON v.digest = i.active_digest "
                "WHERE i.principal_id = ? AND i.plugin_id = ?",
                (principal_id, plugin_id),
            )
        ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "manifest": json.loads(row["manifest_json"]),
            "enabled": bool(row["enabled"]),
            "model_binding": (
                json.loads(row["model_binding_json"])
                if row["model_binding_json"] is not None
                else None
            ),
        }

    async def list_plugin_versions(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT v.version, v.digest, v.signature_status, v.compatibility, "
                "v.state, v.created_at, i.active_digest "
                "FROM plugin_versions v JOIN plugin_installations i "
                "ON i.plugin_id = v.plugin_id "
                "WHERE i.principal_id = ? AND v.plugin_id = ? "
                "ORDER BY v.created_at DESC, v.version DESC",
                (principal_id, plugin_id),
            )
        ).fetchall()
        return [
            {
                "version": row["version"],
                "digest": row["digest"],
                "signature_status": row["signature_status"],
                "compatibility": row["compatibility"],
                "state": row["state"],
                "active": row["digest"] == row["active_digest"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def list_run_plugin_bindings(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT run_id, plugin_id, version, digest, selection_source, required, "
                "command_id, action_catalog_hash, model_binding_json "
                "FROM run_plugin_bindings "
                "WHERE run_id = ? ORDER BY plugin_id",
                (run_id,),
            )
        ).fetchall()
        bindings = []
        for row in rows:
            binding = {**dict(row), "required": bool(row["required"])}
            raw_model_binding = binding.pop("model_binding_json")
            if raw_model_binding is not None:
                binding["model_binding"] = json.loads(raw_model_binding)
            bindings.append(binding)
        return bindings

    @staticmethod
    async def _resolve_run_plugin_bindings(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        plugin_refs: list[dict[str, Any]],
        plugin_command: dict[str, Any] | None,
        inherit_from_run_id: str | None,
    ) -> list[dict[str, Any]]:
        if inherit_from_run_id is not None:
            rows = await (
                await conn.execute(
                    "SELECT b.plugin_id, b.version, b.digest, b.selection_source, "
                    "b.required, b.command_id, b.action_catalog_hash, b.model_binding_json "
                    "FROM run_plugin_bindings b JOIN plugin_versions v "
                    "ON v.plugin_id = b.plugin_id AND v.digest = b.digest "
                    "WHERE b.run_id = ? ORDER BY b.plugin_id",
                    (inherit_from_run_id,),
                )
            ).fetchall()
            return [dict(row) for row in rows]

        rows = await (
            await conn.execute(
                "SELECT i.plugin_id, i.active_digest, i.enabled, i.model_binding_json, "
                "i.retired_at AS installation_retired_at, v.version, v.digest, "
                "v.manifest_json, v.compatibility, v.state "
                "FROM plugin_installations i JOIN plugin_versions v "
                "ON v.plugin_id = i.plugin_id AND v.digest = i.active_digest "
                "WHERE i.principal_id = ?",
                (principal_id,),
            )
        ).fetchall()
        installed = {str(row["plugin_id"]): row for row in rows}
        selected: dict[str, dict[str, Any]] = {}

        def require_available(plugin_id: str, expected_digest: str | None) -> aiosqlite.Row:
            row = installed.get(plugin_id)
            if row is None or row["installation_retired_at"] is not None:
                raise RunAdmissionError("plugin_not_found", f"plugin {plugin_id} is not installed")
            if not bool(row["enabled"]):
                raise RunAdmissionError("plugin_disabled", f"plugin {plugin_id} is disabled")
            if row["compatibility"] != "compatible":
                raise RunAdmissionError(
                    "plugin_incompatible", f"plugin {plugin_id} is incompatible"
                )
            if row["state"] == "retired":
                raise RunAdmissionError("plugin_retired", f"plugin {plugin_id} is retired")
            if expected_digest is not None and expected_digest != row["digest"]:
                raise RunAdmissionError(
                    "plugin_digest_mismatch", f"plugin {plugin_id} active digest changed"
                )
            return row

        def binding(
            row: aiosqlite.Row,
            *,
            selection_source: str,
            required: bool,
            command_id: str | None = None,
        ) -> dict[str, Any]:
            manifest = json.loads(str(row["manifest_json"]))
            command = next(
                (
                    item
                    for item in manifest["contributions"].get("commands", [])
                    if item["id"] == command_id
                ),
                None,
            )
            return {
                "plugin_id": str(row["plugin_id"]),
                "display_name": str(manifest["name"]),
                "version": str(row["version"]),
                "digest": str(row["digest"]),
                "selection_source": selection_source,
                "required": int(required),
                "command_id": command_id,
                "command_title": str(command["title"]) if command is not None else None,
                "action_catalog_hash": plugin_action_catalog_hash(
                    manifest,
                    plugin_digest=str(row["digest"]),
                ),
                "model_binding_json": row["model_binding_json"],
            }

        for row in rows:
            if (
                bool(row["enabled"])
                and row["compatibility"] == "compatible"
                and row["state"] != "retired"
                and row["installation_retired_at"] is None
            ):
                selected[str(row["plugin_id"])] = binding(
                    row,
                    selection_source="enabled",
                    required=False,
                )

        for reference in plugin_refs:
            plugin_id = str(reference["plugin_id"])
            row = require_available(plugin_id, reference.get("expected_digest"))
            selected[plugin_id] = binding(
                row,
                selection_source="explicit",
                required=bool(reference.get("required", True)),
            )

        if plugin_command is not None:
            plugin_id = str(plugin_command["plugin_id"])
            command_id = str(plugin_command["command_id"])
            row = require_available(plugin_id, plugin_command.get("expected_digest"))
            manifest = json.loads(str(row["manifest_json"]))
            commands = manifest["contributions"].get("commands", [])
            if not any(command["id"] == command_id for command in commands):
                raise RunAdmissionError(
                    "plugin_command_not_found",
                    f"plugin {plugin_id} does not contribute command {command_id}",
                )
            selected[plugin_id] = binding(
                row,
                selection_source="command",
                required=True,
                command_id=command_id,
            )

        return [selected[plugin_id] for plugin_id in sorted(selected)]

    async def set_plugin_enabled_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        plugin_id: str,
        expected_digest: str | None,
        enabled: bool,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {"type": command_type, "plugin_id": plugin_id}
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                plugin = await (
                    await conn.execute(
                        "SELECT i.active_digest, i.enabled, i.retired_at, "
                        "i.model_binding_json, v.compatibility, v.state, v.manifest_json "
                        "FROM plugin_installations i JOIN plugin_versions v "
                        "ON v.digest = i.active_digest "
                        "WHERE i.principal_id = ? AND i.plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if plugin is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                digest = str(plugin["active_digest"])
                if expected_digest is not None and expected_digest != digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                if enabled and plugin["compatibility"] != "compatible":
                    raise PluginStateError(
                        "plugin_incompatible", "plugin is incompatible with this Runtime"
                    )
                if enabled and (plugin["retired_at"] is not None or plugin["state"] == "retired"):
                    raise PluginStateError("plugin_retired", "retired plugin cannot be enabled")
                manifest = json.loads(str(plugin["manifest_json"]))
                needs_model_binding = any(
                    "model.vision.invoke" in action.get("capabilities", [])
                    for action in manifest.get("contributions", {}).get("actions", [])
                    if isinstance(action, dict)
                )
                if enabled and needs_model_binding and not plugin["model_binding_json"]:
                    raise PluginStateError(
                        "plugin_model_binding_required",
                        "plugin requires an explicit Vision model binding before enablement",
                    )
                if bool(plugin["enabled"]) != enabled:
                    await conn.execute(
                        "UPDATE plugin_installations SET enabled = ?, revision = revision + 1, "
                        "updated_at = ? WHERE principal_id = ? AND plugin_id = ?",
                        (int(enabled), _now(), principal_id, plugin_id),
                    )
                receipt = {
                    "type": command_type,
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "digest": digest,
                    "enabled": enabled,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        _now(),
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def bind_plugin_model_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        binding_id: str,
        requested_model: str,
        model_binding: dict[str, Any],
        expected_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "plugin.model.bind",
            "plugin_id": plugin_id,
            "binding_id": binding_id,
            "model": requested_model,
        }
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        frozen_binding = {**model_binding, "id": binding_id}
        binding_json = _encode_payload(frozen_binding)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.model.bind",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                plugin = await (
                    await conn.execute(
                        "SELECT i.active_digest, i.retired_at, i.model_binding_json, "
                        "i.model_binding_revision, v.manifest_json, v.execution_kind, v.state "
                        "FROM plugin_installations i JOIN plugin_versions v "
                        "ON v.digest = i.active_digest "
                        "WHERE i.principal_id = ? AND i.plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if plugin is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                digest = str(plugin["active_digest"])
                if expected_digest is not None and expected_digest != digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                if plugin["retired_at"] is not None or plugin["state"] == "retired":
                    raise PluginStateError("plugin_retired", "retired plugin cannot be configured")
                manifest = json.loads(str(plugin["manifest_json"]))
                if plugin["execution_kind"] != "managed_worker" or not any(
                    "model.vision.invoke" in action.get("capabilities", [])
                    for action in manifest["contributions"]["actions"]
                ):
                    raise PluginStateError(
                        "plugin_capability_denied",
                        "plugin does not declare model.vision.invoke",
                    )
                revision = int(plugin["model_binding_revision"])
                if plugin["model_binding_json"] != binding_json:
                    revision += 1
                    await conn.execute(
                        "UPDATE plugin_installations SET model_binding_json = ?, "
                        "model_binding_revision = ?, revision = revision + 1, updated_at = ? "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (binding_json, revision, now, principal_id, plugin_id),
                    )
                summary = {
                    "id": binding_id,
                    "requested_model": requested_model,
                    "connection_id": str(model_binding["connection_id"]),
                    "connection_version": int(model_binding["connection_version"]),
                    "model_id": str(model_binding["model_id"]),
                }
                receipt = {
                    "type": "plugin.model.bind",
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "digest": digest,
                    "model_binding_revision": revision,
                    "model_binding": summary,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plugin.model.bind', '', ?, ?, NULL, ?)",
                    (principal_id, command_id, payload_json, _encode_payload(receipt), now),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def update_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_payload: dict[str, Any],
        plugin_id: str,
        manifest: dict[str, Any],
        digest: str,
        signature_status: str,
        signer_key_id: str | None,
        compatibility: str,
        source: str,
        command_type: str = "plugin.update",
        receipt_type: str = "plugin.update",
        receipt_extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(command_payload)
        version = str(manifest["version"])
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                installation = await (
                    await conn.execute(
                        "SELECT active_digest, enabled, retired_at FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                if installation["retired_at"] is not None:
                    raise PluginStateError("plugin_retired", "retired plugin must be reinstalled")
                previous_digest = str(installation["active_digest"])
                expected_digest = command_payload.get(
                    "expected_digest", command_payload.get("expected_active_digest")
                )
                if expected_digest is not None and expected_digest != previous_digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                if manifest["id"] != plugin_id:
                    raise PluginStateError(
                        "plugin_identity_mismatch", "update package has a different plugin id"
                    )
                if compatibility != "compatible":
                    raise PluginStateError(
                        "plugin_incompatible", "update is incompatible with this Runtime"
                    )
                bound = await (
                    await conn.execute(
                        "SELECT digest FROM plugin_versions WHERE plugin_id = ? AND version = ?",
                        (plugin_id, version),
                    )
                ).fetchone()
                if bound is not None and bound["digest"] != digest:
                    raise PluginVersionConflictError(
                        f"plugin {plugin_id} version {version} already has different content"
                    )
                if bound is None:
                    await conn.execute(
                        "INSERT INTO plugin_versions "
                        "(plugin_id, version, digest, manifest_json, execution_kind, "
                        "signature_status, signer_key_id, compatibility, source, state, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'installed', ?, ?)",
                        (
                            plugin_id,
                            version,
                            digest,
                            _encode_payload(manifest),
                            manifest["runtime"]["execution"]["kind"],
                            signature_status,
                            signer_key_id,
                            compatibility,
                            source,
                            now,
                            now,
                        ),
                    )
                else:
                    await conn.execute(
                        "UPDATE plugin_versions SET state = 'installed', retired_at = NULL, "
                        "updated_at = ? WHERE digest = ?",
                        (now, digest),
                    )
                await conn.execute(
                    "UPDATE plugin_installations SET active_digest = ?, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (digest, now, principal_id, plugin_id),
                )
                receipt = {
                    "type": receipt_type,
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "version": version,
                    "previous_digest": previous_digest,
                    "digest": digest,
                    "enabled": bool(installation["enabled"]),
                }
                if receipt_extra:
                    receipt.update(receipt_extra)
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def rollback_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        target_digest: str,
        expected_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "plugin.rollback",
            "plugin_id": plugin_id,
            "target_digest": target_digest,
        }
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.rollback",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                installation = await (
                    await conn.execute(
                        "SELECT active_digest, enabled, retired_at FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                if installation["retired_at"] is not None:
                    raise PluginStateError("plugin_retired", "retired plugin must be reinstalled")
                previous_digest = str(installation["active_digest"])
                if expected_digest is not None and expected_digest != previous_digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                target = await (
                    await conn.execute(
                        "SELECT version, compatibility FROM plugin_versions "
                        "WHERE plugin_id = ? AND digest = ?",
                        (plugin_id, target_digest),
                    )
                ).fetchone()
                if target is None:
                    raise PluginStateError(
                        "plugin_version_unavailable", "rollback target is not installed"
                    )
                if target["compatibility"] != "compatible":
                    raise PluginStateError("plugin_incompatible", "rollback target is incompatible")
                await conn.execute(
                    "UPDATE plugin_versions SET state = 'installed', retired_at = NULL, "
                    "updated_at = ? WHERE digest = ?",
                    (now, target_digest),
                )
                await conn.execute(
                    "UPDATE plugin_installations SET active_digest = ?, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (target_digest, now, principal_id, plugin_id),
                )
                receipt = {
                    "type": "plugin.rollback",
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "version": target["version"],
                    "previous_digest": previous_digest,
                    "digest": target_digest,
                    "enabled": bool(installation["enabled"]),
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plugin.rollback', '', ?, ?, NULL, ?)",
                    (principal_id, command_id, payload_json, _encode_payload(receipt), now),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def remove_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        expected_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {"type": "plugin.remove", "plugin_id": plugin_id}
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.remove",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                installation = await (
                    await conn.execute(
                        "SELECT active_digest FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                digest = str(installation["active_digest"])
                if expected_digest is not None and expected_digest != digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                await conn.execute(
                    "UPDATE plugin_installations SET enabled = 0, retired_at = ?, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (now, now, principal_id, plugin_id),
                )
                active_count = int(
                    (
                        await (
                            await conn.execute(
                                "SELECT COUNT(*) FROM plugin_installations "
                                "WHERE active_digest = ? AND retired_at IS NULL",
                                (digest,),
                            )
                        ).fetchone()
                    )[0]
                )
                if active_count == 0:
                    await conn.execute(
                        "UPDATE plugin_versions SET state = 'retired', retired_at = ?, "
                        "updated_at = ? WHERE digest = ?",
                        (now, now, digest),
                    )
                receipt = {
                    "type": "plugin.remove",
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "digest": digest,
                    "retired": True,
                    "enabled": False,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plugin.remove', '', ?, ?, NULL, ?)",
                    (principal_id, command_id, payload_json, _encode_payload(receipt), now),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def accepted_run_for_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        client_message_id: str,
        command_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return an immutable command receipt before checking mutable resources."""
        return await self._accepted_run_for_command(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            payload_json=_encode_payload(
                {"client_message_id": client_message_id, "payload": command_payload}
            ),
        )

    async def accept_run_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        client_message_id: str,
        command_payload: dict[str, Any],
        goal: str,
        workspace_path: str | None,
        mode: str,
        thread_id: str | None = None,
        user_input: str | None = None,
        assistant_message_id: str | None = None,
        thread_title: str | None = None,
        thread_metadata: dict[str, Any] | None = None,
        user_item_metadata: dict[str, Any] | None = None,
        replace_from_client_id: str | None = None,
        require_new_thread: bool = False,
        graph_thread_id: str | None = None,
        graph_checkpoint_id: str | None = None,
        graph_definition_id: str | None = None,
        graph_input_kind: str = "new",
        history: list[dict[str, str]] | None = None,
        parent_run_id: str | None = None,
        settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        admission_error: RunAdmissionError | None = None,
        plugin_refs: list[dict[str, Any]] | None = None,
        plugin_command: dict[str, Any] | None = None,
        inherit_plugin_bindings_from: str | None = None,
        run_inputs: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one immutable start command and its queued run."""
        payload_json = _encode_payload(
            {"client_message_id": client_message_id, "payload": command_payload}
        )
        existing = await self._accepted_run_for_command(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            payload_json=payload_json,
        )
        if existing is not None:
            return existing, False
        path_error = (
            await self._workspace_path_error(workspace_path) if admission_error is None else None
        )
        if path_error is not None:
            existing = await self._accepted_run_for_command(
                self._conn,
                principal_id=principal_id,
                command_id=command_id,
                payload_json=payload_json,
            )
            if existing is not None:
                return existing, False
            raise WorkspaceAdmissionError(path_error)

        async with aiosqlite.connect(str(self._db_path)) as transaction_conn:
            await _configure_connection(transaction_conn)
            await transaction_conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_run_for_command(
                    transaction_conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await transaction_conn.rollback()
                    return existing, False
                if admission_error is not None:
                    raise admission_error
                if path_error is not None:
                    raise WorkspaceAdmissionError(path_error)

                workspace_error = await self._workspace_owner_error(
                    transaction_conn,
                    principal_id=principal_id,
                    path=workspace_path,
                )
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                admitted_root_run_id: str | None = None
                if parent_run_id is not None:
                    parent = await (
                        await transaction_conn.execute(
                            "SELECT status, run_kind, root_run_id FROM local_runs "
                            "WHERE principal_id = ? AND id = ?",
                            (principal_id, parent_run_id),
                        )
                    ).fetchone()
                    if parent is None:
                        raise ParentRunAdmissionError("parent run not found")
                    if str(parent["run_kind"]) == "child":
                        raise ParentRunAdmissionError(
                            "a durable child run cannot be used as a conversation parent"
                        )
                    if str(parent["status"]) in {"queued", "running", "cleanup_required"}:
                        raise ParentRunAdmissionError(
                            "parent run has not reached a safely settled state"
                        )
                    admitted_root_run_id = str(parent["root_run_id"] or parent_run_id)

                plugin_bindings = await self._resolve_run_plugin_bindings(
                    transaction_conn,
                    principal_id=principal_id,
                    plugin_refs=plugin_refs or [],
                    plugin_command=plugin_command,
                    inherit_from_run_id=inherit_plugin_bindings_from,
                )
                normalized_user_item_metadata = dict(user_item_metadata or {})
                normalized_user_item_metadata.pop("plugin_selection", None)
                if plugin_refs or plugin_command:
                    bindings_by_id = {
                        str(binding["plugin_id"]): binding for binding in plugin_bindings
                    }
                    references = []
                    seen_references: set[str] = set()
                    for reference in plugin_refs or []:
                        plugin_id = str(reference["plugin_id"])
                        if plugin_id in seen_references:
                            continue
                        binding = bindings_by_id[plugin_id]
                        references.append(
                            {
                                "plugin_id": plugin_id,
                                "name": str(binding["display_name"]),
                                "digest": str(binding["digest"]),
                            }
                        )
                        seen_references.add(plugin_id)
                    selection: dict[str, Any] = {"references": references}
                    if plugin_command is not None:
                        plugin_id = str(plugin_command["plugin_id"])
                        binding = bindings_by_id[plugin_id]
                        selection["command"] = {
                            "plugin_id": plugin_id,
                            "plugin_name": str(binding["display_name"]),
                            "command_id": str(plugin_command["command_id"]),
                            "title": str(binding["command_title"]),
                            "digest": str(binding["digest"]),
                        }
                    normalized_user_item_metadata["plugin_selection"] = selection

                product_thread_id = (
                    _principal_thread_id(principal_id, thread_id)
                    if thread_id is not None
                    else _new_id("thread")
                )
                thread = await (
                    await transaction_conn.execute(
                        "SELECT * FROM local_threads WHERE id = ?",
                        (product_thread_id,),
                    )
                ).fetchone()
                now = _now()
                seed_history: list[dict[str, str]] = []
                if thread is not None and require_new_thread:
                    raise ThreadAdmissionError("fork target thread already exists")
                initial_thread_title: str | None = None
                if thread is None:
                    if replace_from_client_id is not None:
                        raise ThreadAdmissionError("thread not found")
                    initial_thread_title = " ".join((thread_title or user_input or goal).split())[
                        :80
                    ]
                    await transaction_conn.execute(
                        "INSERT INTO local_threads "
                        "(id, principal_id, title, metadata_json, version, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, ?, ?)",
                        (
                            product_thread_id,
                            principal_id,
                            initial_thread_title,
                            _encode_payload(thread_metadata or {}),
                            now,
                            now,
                        ),
                    )
                    thread_version = 1
                    seed_history = [
                        {"role": str(message["role"]), "content": str(message["content"])}
                        for message in history or []
                        if message.get("role") in {"user", "assistant"}
                        and str(message.get("content") or "").strip()
                    ]
                    base_position = len(seed_history)
                else:
                    if thread["principal_id"] != principal_id:
                        raise ThreadAdmissionError("thread not found")
                    if thread["deleted_at"] is not None:
                        raise ThreadAdmissionError("thread not found")
                    if thread["archived_at"] is not None:
                        raise ThreadAdmissionError("thread is archived")
                    duplicate_item = await (
                        await transaction_conn.execute(
                            "SELECT 1 FROM local_thread_items "
                            "WHERE thread_id = ? AND client_id = ?",
                            (product_thread_id, client_message_id),
                        )
                    ).fetchone()
                    if duplicate_item is not None:
                        raise CommandConflictError(
                            f"client message {client_message_id} already belongs to another command"
                        )
                    active = await (
                        await transaction_conn.execute(
                            "SELECT 1 FROM local_runs r "
                            "LEFT JOIN local_run_jobs j ON j.run_id = r.id "
                            "WHERE r.principal_id = ? AND r.thread_id = ? AND ("
                            "r.status NOT IN ('completed', 'failed', 'canceled') "
                            "OR j.status IN ('pending', 'leased')) LIMIT 1",
                            (principal_id, product_thread_id),
                        )
                    ).fetchone()
                    if active is not None:
                        raise ThreadAdmissionError("thread has an unsettled run")
                    if replace_from_client_id is not None:
                        target = await (
                            await transaction_conn.execute(
                                "SELECT position FROM local_thread_items "
                                "WHERE thread_id = ? AND client_id = ? "
                                "AND item_type = 'user_message' AND superseded_at IS NULL",
                                (product_thread_id, replace_from_client_id),
                            )
                        ).fetchone()
                        if target is None:
                            raise ThreadAdmissionError("thread message not found")
                        base_position = int(target["position"]) - 1
                    else:
                        position_row = await (
                            await transaction_conn.execute(
                                "SELECT COALESCE(MAX(position), 0) FROM local_thread_items "
                                "WHERE thread_id = ? AND superseded_at IS NULL",
                                (product_thread_id,),
                            )
                        ).fetchone()
                        base_position = int(position_row[0] if position_row else 0)
                    thread_version = int(thread["version"]) + 1
                    await transaction_conn.execute(
                        "UPDATE local_threads SET version = ?, title = ?, metadata_json = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            thread_version,
                            " ".join((thread_title or thread["title"]).split())[:80],
                            _encode_payload(thread_metadata)
                            if thread_metadata is not None
                            else thread["metadata_json"],
                            now,
                            product_thread_id,
                        ),
                    )

                if thread is None:
                    authoritative_history = seed_history
                    if seed_history:
                        await transaction_conn.executemany(
                            "INSERT INTO local_thread_items "
                            "(id, thread_id, run_id, client_id, item_type, status, content, "
                            "metadata_json, position, version, created_at, updated_at, completed_at) "
                            "VALUES (?, ?, NULL, NULL, ?, 'completed', ?, '{}', ?, 1, ?, ?, ?)",
                            [
                                (
                                    _new_id("item"),
                                    product_thread_id,
                                    f"{message['role']}_message",
                                    message["content"],
                                    position,
                                    now,
                                    now,
                                    now,
                                )
                                for position, message in enumerate(seed_history, start=1)
                            ],
                        )
                else:
                    history_rows = await (
                        await transaction_conn.execute(
                            "SELECT item_type, content FROM local_thread_items "
                            "WHERE thread_id = ? AND superseded_at IS NULL "
                            "AND position <= ? AND item_type IN "
                            "('user_message', 'assistant_message') AND content != '' "
                            "ORDER BY position, id",
                            (product_thread_id, base_position),
                        )
                    ).fetchall()
                    authoritative_history = [
                        {
                            "role": "user" if row["item_type"] == "user_message" else "assistant",
                            "content": str(row["content"]),
                        }
                        for row in history_rows
                    ]

                assistant_item_id = (
                    _principal_item_id(principal_id, assistant_message_id)
                    if assistant_message_id is not None
                    else _new_id("item")
                )
                run = self._new_run_record(
                    principal_id=principal_id,
                    goal=goal,
                    workspace_path=workspace_path,
                    parent_run_id=parent_run_id,
                    settings=settings,
                    metadata=metadata,
                    mode=mode,
                    history=authoritative_history,
                    thread_id=product_thread_id,
                    assistant_item_id=assistant_item_id,
                    user_input=user_input,
                    graph_thread_id=graph_thread_id,
                    graph_checkpoint_id=graph_checkpoint_id,
                    graph_definition_id=graph_definition_id,
                    graph_input_kind=graph_input_kind,
                    run_kind="fork" if graph_input_kind == "fork" else "turn",
                    root_run_id=admitted_root_run_id,
                )
                await self._insert_run(transaction_conn, run)
                if run_inputs:
                    await transaction_conn.executemany(
                        "INSERT INTO local_run_inputs "
                        "(run_id, input_id, virtual_path, original_name, media_type, bytes, "
                        "sha256, blob_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                run["id"],
                                item["input_id"],
                                item["virtual_path"],
                                item["original_name"],
                                item["media_type"],
                                item["bytes"],
                                item["sha256"],
                                item["blob_key"],
                                now,
                            )
                            for item in run_inputs
                        ],
                    )
                if plugin_bindings:
                    await transaction_conn.executemany(
                        "INSERT INTO run_plugin_bindings "
                        "(run_id, plugin_id, version, digest, selection_source, required, "
                        "command_id, action_catalog_hash, model_binding_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                run["id"],
                                item["plugin_id"],
                                item["version"],
                                item["digest"],
                                item["selection_source"],
                                item["required"],
                                item["command_id"],
                                item["action_catalog_hash"],
                                item.get("model_binding_json"),
                            )
                            for item in plugin_bindings
                        ],
                    )
                if replace_from_client_id is not None:
                    await transaction_conn.execute(
                        "UPDATE local_thread_items SET superseded_at = ?, "
                        "superseded_by_run_id = ?, updated_at = ? "
                        "WHERE thread_id = ? AND position > ? AND superseded_at IS NULL",
                        (now, run["id"], now, product_thread_id, base_position),
                    )
                await transaction_conn.executemany(
                    "INSERT INTO local_thread_items "
                    "(id, thread_id, run_id, client_id, item_type, status, content, "
                    "metadata_json, position, version, created_at, updated_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        (
                            _new_id("item"),
                            product_thread_id,
                            run["id"],
                            client_message_id,
                            "user_message",
                            "completed",
                            user_input or goal,
                            _encode_payload(normalized_user_item_metadata),
                            base_position + 1,
                            now,
                            now,
                            now,
                        ),
                        (
                            assistant_item_id,
                            product_thread_id,
                            run["id"],
                            assistant_message_id,
                            "assistant_message",
                            "in_progress",
                            "",
                            "{}",
                            base_position + 2,
                            now,
                            now,
                            None,
                        ),
                    ),
                )
                await transaction_conn.execute(
                    "INSERT INTO local_thread_changes "
                    "(principal_id, thread_id, thread_version, change_type, run_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        principal_id,
                        product_thread_id,
                        thread_version,
                        "thread.rewritten"
                        if replace_from_client_id is not None
                        else "turn.started",
                        run["id"],
                        now,
                    ),
                )
                await self._insert_run_job(
                    transaction_conn,
                    self._new_run_job_record(
                        run_id=run["id"],
                        kind="start",
                        input_payload={
                            "principal_id": principal_id,
                            "goal": goal,
                            "user_input": user_input or goal,
                            "workspace_path": workspace_path,
                            "mode": mode,
                            "history": authoritative_history,
                            "settings": settings or {},
                            "metadata": metadata or {},
                        },
                    ),
                )
                await transaction_conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        str(command_payload.get("type") or "run.start"),
                        client_message_id,
                        payload_json,
                        _encode_payload(run),
                        run["id"],
                        run["created_at"],
                    ),
                )
                await transaction_conn.commit()
                return {
                    **run,
                    "command_id": command_id,
                    "client_message_id": client_message_id,
                }, True
            except BaseException:
                await transaction_conn.rollback()
                raise

    async def run_initial_thread_title_seed(self, run_id: str) -> str | None:
        row = await (
            await self._conn.execute(
                "SELECT c.payload_json, r.user_input, r.goal FROM local_runs r "
                "LEFT JOIN local_commands c ON c.run_id = r.id "
                "AND c.command_type IN ('run.start', 'run.fork') WHERE r.id = ? "
                "ORDER BY c.created_at LIMIT 1",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            return None
        try:
            envelope = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            envelope = {}
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        requested = payload.get("thread_title") if isinstance(payload, dict) else None
        return " ".join(str(requested or row["user_input"] or row["goal"] or "").split())[:80]

    async def enqueue_run_job(
        self,
        run_id: str,
        *,
        kind: str,
        resume_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        existing_run = await self.get_run(run_id)
        if existing_run is None:
            return None
        path_error = await self._workspace_path_error(existing_run.get("workspace_path"))
        if path_error is not None:
            raise WorkspaceAdmissionError(path_error)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute("SELECT * FROM local_runs WHERE id = ?", (run_id,))
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                run = dict(row)
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=str(run["principal_id"]),
                    path=run["workspace_path"],
                )
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                active = await (
                    await conn.execute(
                        "SELECT * FROM local_run_jobs WHERE run_id = ? "
                        "AND status IN ('pending', 'leased')",
                        (run_id,),
                    )
                ).fetchone()
                if active is not None:
                    await conn.rollback()
                    return dict(active)
                job = self._new_run_job_record(
                    run_id=run_id,
                    kind=kind,
                    input_payload=self._run_job_input(run),
                    resume_payload=resume_payload,
                )
                await self._insert_run_job(conn, job)
                await conn.commit()
                return job
            except BaseException:
                await conn.rollback()
                raise

    async def get_active_run_job(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_run_jobs WHERE run_id = ? "
            "AND status IN ('pending', 'leased') AND quarantined_at IS NULL",
            (run_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_run_job(self, job_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_run_jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _sandbox_cleanup_report_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        execution_attempt_id: str,
    ) -> dict[str, Any]:
        """Report whether this attempt's sandboxes are known to have stopped.

        Confirmation requires evidence, so an attempt that recorded no sandbox
        stays unconfirmed rather than claiming a clean exit it cannot show. A
        record left stale is likewise unconfirmed: the pid was recycled, so
        whatever it named was never proven to have stopped.
        """

        cursor = await conn.execute(
            "SELECT status, COUNT(*) AS total FROM local_sandbox_processes "
            "WHERE run_id = ? AND execution_attempt_id = ? GROUP BY status",
            (run_id, execution_attempt_id),
        )
        counts = {str(row["status"]): int(row["total"]) for row in await cursor.fetchall()}
        if not counts:
            return {"status": "unconfirmed"}
        stopped = counts.get("reaped", 0) + counts.get("gone", 0)
        if stopped == sum(counts.values()):
            return {"status": "completed", "sandboxes_stopped": stopped}
        return {
            "status": "unconfirmed",
            "sandboxes_stopped": stopped,
            "sandboxes_unaccounted": sum(counts.values()) - stopped,
        }

    async def _requeue_expired_jobs_uncommitted(
        self,
        conn: aiosqlite.Connection,
        now: str,
    ) -> None:
        expired = await (
            await conn.execute(
                "SELECT local_run_jobs.*, local_runs.status AS run_status "
                "FROM local_run_jobs JOIN local_runs ON local_runs.id = local_run_jobs.run_id "
                "WHERE local_run_jobs.status = 'leased' "
                "AND local_run_jobs.lease_expires_at <= ? "
                "ORDER BY local_run_jobs.lease_expires_at, local_run_jobs.id",
                (now,),
            )
        ).fetchall()
        for expired_row in expired:
            job = dict(expired_row)
            execution_attempt_id = f"{job['id']}:{job['lease_generation']}"
            await conn.execute(
                "UPDATE local_model_calls SET status = 'outcome_unknown', completed_at = ? "
                "WHERE run_id = ? AND execution_attempt_id = ? "
                "AND status IN ('reserved', 'streaming')",
                (now, job["run_id"], execution_attempt_id),
            )
            await self._mark_running_tool_receipts_outcome_unknown_uncommitted(
                conn,
                run_id=str(job["run_id"]),
                execution_attempt_id=execution_attempt_id,
                error_type="execution_lease_expired",
                now=now,
            )
            # Only flag them here. Killing is an irreversible side effect and
            # this runs inside BEGIN IMMEDIATE, so it must not happen while the
            # write lock is held or a rollback would leave processes already
            # dead. The reaper picks these up outside the transaction.
            await conn.execute(
                "UPDATE local_sandbox_processes SET status = 'orphaned', updated_at = ? "
                "WHERE run_id = ? AND execution_attempt_id = ? AND status = 'running'",
                (now, job["run_id"], execution_attempt_id),
            )
            settled_job_status = {
                "completed": "completed",
                "failed": "dead",
                "canceled": "canceled",
                "waiting_permission": "completed",
                "waiting_input": "completed",
            }.get(str(job["run_status"]))
            if settled_job_status is not None:
                run_status = str(job["run_status"])
                if run_status in _TERMINAL_RUN_STATUSES:
                    await self._cancel_unstarted_tool_receipts_uncommitted(
                        conn,
                        run_id=str(job["run_id"]),
                        canceled_at=now,
                        error_type={
                            "completed": "ParentRunCompleted",
                            "failed": "ParentRunFailed",
                            "canceled": "RunCanceled",
                        }[run_status],
                    )
                await conn.execute(
                    "UPDATE local_run_jobs SET status = ?, updated_at = ?, finished_at = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE id = ? AND status = 'leased'",
                    (settled_job_status, now, now, job["id"]),
                )
                continue
            await self._cancel_unstarted_tool_receipts_uncommitted(
                conn,
                run_id=str(job["run_id"]),
                canceled_at=now,
                error_type="ParentRunCleanupRequired",
            )
            await conn.execute(
                "UPDATE local_run_jobs SET quarantined_at = ?, "
                "quarantine_reason = 'execution_lease_expired', "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'leased'",
                (now, now, job["id"]),
            )
            await conn.execute(
                "UPDATE local_runs SET status = 'cleanup_required', updated_at = ?, "
                "completed_at = NULL "
                "WHERE id = ?",
                (now, job["run_id"]),
            )
            cleanup_report = await self._sandbox_cleanup_report_uncommitted(
                conn,
                run_id=str(job["run_id"]),
                execution_attempt_id=execution_attempt_id,
            )
            cleanup_payload = {
                "error": _lease_expiry_error(cleanup_report),
                "type": "ExecutionLeaseExpiredError",
                "retryable": False,
                "category": "execution_lease_expired",
                "cleanup": cleanup_report,
            }
            event = await self._append_event_uncommitted(
                conn,
                job["run_id"],
                "run.cleanup_required",
                payload_json=_encode_payload(cleanup_payload),
                created_at=now,
            )
            await self._update_thread_projection_uncommitted(
                conn,
                run_id=job["run_id"],
                run_status="cleanup_required",
                change_type="run.cleanup_required",
                payload=cleanup_payload,
                event_high_watermark=int(event["seq"]),
                changed_at=now,
            )

    async def claim_run_job(
        self,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                await self._requeue_expired_jobs_uncommitted(conn, now)
                blocked_rows = await (
                    await conn.execute(
                        "SELECT DISTINCT j.run_id FROM local_run_jobs j "
                        "JOIN local_child_dependencies d ON d.child_run_id = j.run_id "
                        "JOIN local_runs dependency ON dependency.id = d.dependency_run_id "
                        "WHERE j.status = 'pending' "
                        "AND dependency.status IN ('failed', 'canceled', 'cleanup_required') "
                        "ORDER BY j.created_at, j.id"
                    )
                ).fetchall()
                for blocked in blocked_rows:
                    await self._request_run_cancel_uncommitted(conn, str(blocked["run_id"]))
                row = await (
                    await conn.execute(
                        "SELECT j.* FROM local_run_jobs j JOIN local_runs r ON r.id = j.run_id "
                        "WHERE j.status = 'pending' AND (r.run_kind != 'child' OR NOT EXISTS ("
                        "SELECT 1 FROM local_child_dependencies d "
                        "JOIN local_runs dependency ON dependency.id = d.dependency_run_id "
                        "WHERE d.child_run_id = j.run_id AND dependency.status != 'completed'"
                        ")) ORDER BY j.created_at, j.id LIMIT 1"
                    )
                ).fetchone()
                if row is None:
                    await conn.commit()
                    return None
                job = dict(row)
                expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
                await conn.execute(
                    "UPDATE local_run_jobs SET status = 'leased', lease_owner = ?, "
                    "lease_generation = lease_generation + 1, lease_expires_at = ?, "
                    "attempt = attempt + 1, updated_at = ? WHERE id = ? AND status = 'pending'",
                    (worker_id, expires_at, now, job["id"]),
                )
                await conn.execute(
                    "UPDATE local_runs SET status = 'running', updated_at = ?, completed_at = NULL "
                    "WHERE id = ?",
                    (now, job["run_id"]),
                )
                claimed = await (
                    await conn.execute("SELECT * FROM local_run_jobs WHERE id = ?", (job["id"],))
                ).fetchone()
                await conn.commit()
                return dict(claimed) if claimed else None
            except BaseException:
                await conn.rollback()
                raise

    async def renew_run_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        lease_seconds: float = 30.0,
    ) -> tuple[bool, bool]:
        renewed_at = _now()
        expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "UPDATE local_run_jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_owner = ? "
                "AND lease_generation = ? AND quarantined_at IS NULL "
                "AND lease_expires_at > ?",
                (
                    expires_at,
                    renewed_at,
                    job_id,
                    lease_owner,
                    lease_generation,
                    renewed_at,
                ),
            )
            if cursor.rowcount != 1:
                await conn.rollback()
                return False, False
            row = await (
                await conn.execute(
                    "SELECT cancel_requested_at FROM local_run_jobs WHERE id = ?", (job_id,)
                )
            ).fetchone()
            await conn.commit()
            return True, bool(row and row[0])

    async def quarantine_execution_attempt(
        self,
        run_id: str,
        *,
        reason: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Fence an attempt whose resource stillness cannot be proven.

        The job intentionally remains leased to its exact owner/generation,
        but with no renewable expiry. It therefore blocks new attempts while
        allowing that original owner to submit a final cleanup confirmation.
        """
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != run_id:
            raise LeaseFenceError("execution quarantine requires the current run lease")
        now = _now()
        async with self.run_write_transaction(run_id, lease=lease) as conn:
            execution_attempt_id = f"{lease.job_id}:{lease.lease_generation}"
            await conn.execute(
                "UPDATE local_model_calls SET status = 'outcome_unknown', completed_at = ? "
                "WHERE run_id = ? AND execution_attempt_id = ? "
                "AND status IN ('reserved', 'streaming')",
                (now, run_id, execution_attempt_id),
            )
            await self._mark_running_tool_receipts_outcome_unknown_uncommitted(
                conn,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                error_type=reason,
                now=now,
            )
            await self._cancel_unstarted_tool_receipts_uncommitted(
                conn,
                run_id=run_id,
                canceled_at=now,
                error_type="ParentRunCleanupRequired",
            )
            await conn.execute(
                "UPDATE local_runs SET status = 'cleanup_required', updated_at = ?, "
                "completed_at = NULL WHERE id = ?",
                (now, run_id),
            )
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                "run.cleanup_required",
                payload_json=_encode_payload(payload),
                created_at=now,
            )
            await self._update_thread_projection_uncommitted(
                conn,
                run_id=run_id,
                run_status="cleanup_required",
                change_type="run.cleanup_required",
                payload=payload,
                event_high_watermark=int(event["seq"]),
                changed_at=now,
            )
            cursor = await conn.execute(
                "UPDATE local_run_jobs SET quarantined_at = ?, quarantine_reason = ?, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND run_id = ? "
                "AND status = 'leased' AND lease_owner = ? AND lease_generation = ?",
                (
                    now,
                    reason,
                    now,
                    lease.job_id,
                    run_id,
                    lease.lease_owner,
                    lease.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseFenceError("execution attempt could not be quarantined")
            return event

    async def confirm_quarantined_cleanup(
        self,
        run_id: str,
        *,
        job_id: str,
        lease_owner: str,
        lease_generation: int,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Let only the quarantined owner close its attempt after cleanup."""
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT 1 FROM local_run_jobs WHERE id = ? AND run_id = ? "
                        "AND status = 'leased' AND lease_owner = ? AND lease_generation = ? "
                        "AND quarantined_at IS NOT NULL",
                        (job_id, run_id, lease_owner, lease_generation),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                await self._cancel_unstarted_tool_receipts_uncommitted(
                    conn,
                    run_id=run_id,
                    canceled_at=now,
                    error_type="ParentRunCleanupRequired",
                )
                await conn.execute(
                    "UPDATE local_runs SET status = 'failed', updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND status = 'cleanup_required'",
                    (now, now, run_id),
                )
                event = await self._append_event_uncommitted(
                    conn,
                    run_id,
                    "run.failed",
                    payload_json=_encode_payload(payload),
                    created_at=now,
                )
                await self._update_thread_projection_uncommitted(
                    conn,
                    run_id=run_id,
                    run_status="failed",
                    change_type="run.failed",
                    payload=payload,
                    event_high_watermark=int(event["seq"]),
                    changed_at=now,
                )
                await conn.execute(
                    "UPDATE local_run_jobs SET status = 'dead', updated_at = ?, "
                    "finished_at = ?, lease_expires_at = NULL WHERE id = ?",
                    (now, now, job_id),
                )
                await conn.commit()
                return event
            except BaseException:
                await conn.rollback()
                raise

    async def ensure_lost_execution_quarantined(
        self,
        run_id: str,
        *,
        job_id: str,
        lease_owner: str,
        lease_generation: int,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Atomically record lease loss before an exact owner confirms cleanup.

        Heartbeat can observe expiry before the dispatcher/reaper does. This
        method closes that ordering window: the exact old generation may turn
        its expired lease into quarantine, settle uncertain ledgers, and then
        submit its already-completed cleanup proof. A crash between these two
        calls leaves a safe quarantine rather than a claimable job.
        """
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT * FROM local_run_jobs WHERE id = ? AND run_id = ? "
                        "AND status = 'leased' AND lease_owner = ? AND lease_generation = ?",
                        (job_id, run_id, lease_owner, lease_generation),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return False, None
                job = dict(row)
                already_quarantined = job.get("quarantined_at") is not None
                if not already_quarantined and str(job.get("lease_expires_at") or "") > now:
                    await conn.rollback()
                    return False, None
                execution_attempt_id = f"{job_id}:{lease_generation}"
                await conn.execute(
                    "UPDATE local_model_calls SET status = 'outcome_unknown', completed_at = ? "
                    "WHERE run_id = ? AND execution_attempt_id = ? "
                    "AND status IN ('reserved', 'streaming')",
                    (now, run_id, execution_attempt_id),
                )
                await self._mark_running_tool_receipts_outcome_unknown_uncommitted(
                    conn,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    error_type="execution_lease_expired",
                    now=now,
                )
                await self._cancel_unstarted_tool_receipts_uncommitted(
                    conn,
                    run_id=run_id,
                    canceled_at=now,
                    error_type="ParentRunCleanupRequired",
                )
                await conn.execute(
                    "UPDATE local_sandbox_processes SET status = 'orphaned', updated_at = ? "
                    "WHERE run_id = ? AND execution_attempt_id = ? AND status = 'running'",
                    (now, run_id, execution_attempt_id),
                )
                lease_cleanup_report = await self._sandbox_cleanup_report_uncommitted(
                    conn,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                )
                event: dict[str, Any] | None = None
                if not already_quarantined:
                    await conn.execute(
                        "UPDATE local_run_jobs SET quarantined_at = ?, "
                        "quarantine_reason = 'execution_lease_expired', "
                        "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                        (now, now, job_id),
                    )
                    await conn.execute(
                        "UPDATE local_runs SET status = 'cleanup_required', updated_at = ?, "
                        "completed_at = NULL WHERE id = ?",
                        (now, run_id),
                    )
                    event = await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "run.cleanup_required",
                        payload_json=_encode_payload(
                            {
                                "error": _lease_expiry_error(lease_cleanup_report),
                                "type": "ExecutionLeaseExpiredError",
                                "retryable": False,
                                "category": "execution_lease_expired",
                                "cleanup": lease_cleanup_report,
                            }
                        ),
                        created_at=now,
                    )
                    await self._update_thread_projection_uncommitted(
                        conn,
                        run_id=run_id,
                        run_status="cleanup_required",
                        change_type="run.cleanup_required",
                        payload={
                            "error": _lease_expiry_error(lease_cleanup_report),
                            "type": "ExecutionLeaseExpiredError",
                            "retryable": False,
                            "category": "execution_lease_expired",
                            "cleanup": lease_cleanup_report,
                        },
                        event_high_watermark=int(event["seq"]),
                        changed_at=now,
                    )
                await conn.commit()
                return True, event
            except BaseException:
                await conn.rollback()
                raise

    async def finish_run_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        status: str,
    ) -> bool:
        if status not in {"completed", "canceled", "dead"}:
            raise ValueError(f"invalid finished job status: {status}")
        finished_at = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "UPDATE local_run_jobs SET status = ?, updated_at = ?, finished_at = ?, "
                "lease_expires_at = NULL WHERE id = ? AND status = 'leased' "
                "AND lease_owner = ? AND lease_generation = ? AND lease_expires_at > ?",
                (
                    status,
                    finished_at,
                    finished_at,
                    job_id,
                    lease_owner,
                    lease_generation,
                    finished_at,
                ),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def request_run_cancel(self, run_id: str) -> str | None:
        states = await self.request_run_cancel_tree(run_id)
        return states.get(run_id)

    async def request_run_cancel_tree(self, run_id: str) -> dict[str, str]:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                states = await self._request_run_cancel_tree_uncommitted(conn, run_id)
                if (
                    not states
                    and await (
                        await conn.execute("SELECT 1 FROM local_runs WHERE id = ?", (run_id,))
                    ).fetchone()
                    is None
                ):
                    await conn.rollback()
                    return {}
                await conn.commit()
                return states
            except BaseException:
                await conn.rollback()
                raise

    async def request_run_cancel_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload({"type": "run.cancel", "run_id": run_id})
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="run.cancel",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                run = await (
                    await conn.execute(
                        "SELECT id, created_at FROM local_runs WHERE principal_id = ? AND id = ?",
                        (principal_id, run_id),
                    )
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown run: {run_id}")
                states = await self._request_run_cancel_tree_uncommitted(conn, run_id)
                receipt = {
                    "type": "run.cancel",
                    "command_id": command_id,
                    "run_id": run_id,
                    "canceled": run_id in states,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, 'run.cancel', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        _now(),
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def _request_run_cancel_tree_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
    ) -> dict[str, str]:
        rows = await (
            await conn.execute(
                "WITH RECURSIVE tree(id, depth) AS ("
                "SELECT id, 0 FROM local_runs WHERE id = ? UNION ALL "
                "SELECT r.id, tree.depth + 1 FROM local_runs r "
                "JOIN tree ON r.parent_run_id = tree.id WHERE r.run_kind = 'child'"
                ") SELECT id FROM tree ORDER BY depth DESC, id",
                (run_id,),
            )
        ).fetchall()
        states: dict[str, str] = {}
        for row in rows:
            target_run_id = str(row["id"])
            state = await self._request_run_cancel_uncommitted(conn, target_run_id)
            if state is not None:
                states[target_run_id] = state
        return states

    async def request_question_answer_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        question_id: str,
        answers: dict[str, list[str]],
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(
            {"type": "question.answer", "question_id": question_id, "answers": answers}
        )
        answers_json = json.dumps(
            answers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="question.answer",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT q.run_id, q.wait_cycle_id, q.status AS question_status, "
                        "q.answers_json, r.status AS run_status, r.principal_id, "
                        "r.goal, r.user_input, r.workspace_path, r.mode, r.history_json, "
                        "r.settings_json, r.metadata_json, r.id AS id, r.run_kind, "
                        "r.root_run_id, r.agent_definition_id, r.agent_definition_version, "
                        "r.collaboration_depth, r.collaboration_policy_json "
                        "FROM local_questions q JOIN local_runs r ON r.id = q.run_id "
                        "WHERE q.id = ? AND r.principal_id = ?",
                        (question_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown question: {question_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)

                if record["question_status"] == "pending":
                    if record["run_status"] not in {"waiting_permission", "waiting_input"}:
                        raise WaitDecisionConflictError("run is not awaiting a question answer")
                    now = _now()
                    question_cursor = await conn.execute(
                        "UPDATE local_questions SET status = 'answered', answers_json = ?, "
                        "answered_at = ? WHERE id = ? AND status = 'pending'",
                        (answers_json, now, question_id),
                    )
                    wait_cursor = await conn.execute(
                        "UPDATE local_wait_candidates SET status = 'resolved', "
                        "decision_json = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (answers_json, now, question_id),
                    )
                    if question_cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                        raise WaitDecisionConflictError("question was answered concurrently")
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "question.answered",
                        payload_json=_encode_payload(
                            {"request_id": question_id, "answers": answers}
                        ),
                        created_at=now,
                    )
                elif (
                    record["question_status"] != "answered"
                    or str(record["answers_json"] or "") != answers_json
                ):
                    raise WaitDecisionConflictError(
                        "question was already resolved with different content"
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(record["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "question.answer",
                    "command_id": command_id,
                    "question_id": question_id,
                    "run_id": run_id,
                    "answered": True,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'question.answer', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        _now(),
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def request_permission_resolve_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        permission_id: str,
        decision: str,
        scope: str,
        edited_action: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "permission.resolve",
            "permission_id": permission_id,
            "decision": decision,
            "scope": scope,
        }
        if edited_action is not None:
            command_payload["edited_action"] = edited_action
        payload_json = _encode_payload(command_payload)
        if decision == "approve":
            hitl_decision: dict[str, Any] = {"type": "approve"}
            permission_status = "approved"
        elif decision == "edit":
            hitl_decision = {"type": "edit", "edited_action": edited_action}
            permission_status = "approved"
        else:
            hitl_decision = {
                "type": "reject",
                "message": "Tool execution denied by user.",
            }
            permission_status = "denied"
        decision_json = _encode_payload(hitl_decision)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="permission.resolve",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT p.run_id, p.wait_cycle_id, p.status AS permission_status, "
                        "p.scope AS permission_scope, p.decision_json, p.tool_name, p.risk, "
                        "p.operation_id, r.status AS run_status, r.principal_id, "
                        "r.goal, r.user_input, r.workspace_path, r.mode, r.history_json, "
                        "r.settings_json, r.metadata_json, r.id AS id, r.run_kind, "
                        "r.root_run_id, r.agent_definition_id, r.agent_definition_version, "
                        "r.collaboration_depth, r.collaboration_policy_json "
                        "FROM local_permissions p JOIN local_runs r ON r.id = p.run_id "
                        "WHERE p.id = ? AND r.principal_id = ?",
                        (permission_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown permission: {permission_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                if decision == "edit" and (
                    edited_action is None or edited_action.get("name") != record["tool_name"]
                ):
                    raise WaitDecisionConflictError("tool name cannot be changed")
                require_allowed_permission_scope(
                    tool_name=str(record["tool_name"]),
                    risk=record["risk"],
                    status=permission_status,
                    scope=scope,
                )
                grant_max_uses = -1 if scope == "run" and permission_status == "approved" else 0
                grant_expires_at = None

                if record["permission_status"] == "pending":
                    if record["run_status"] not in {"waiting_permission", "waiting_input"}:
                        raise WaitDecisionConflictError("run is not awaiting a permission decision")
                    now = _now()
                    permission_cursor = await conn.execute(
                        "UPDATE local_permissions SET status = ?, scope = ?, decision_json = ?, "
                        "grant_max_uses = ?, grant_expires_at = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (
                            permission_status,
                            scope,
                            decision_json,
                            grant_max_uses,
                            grant_expires_at,
                            now,
                            permission_id,
                        ),
                    )
                    wait_cursor = await conn.execute(
                        "UPDATE local_wait_candidates SET status = 'resolved', "
                        "decision_json = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (decision_json, now, permission_id),
                    )
                    if permission_cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                        raise WaitDecisionConflictError("permission was resolved concurrently")
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "permission.resolved",
                        payload_json=_encode_payload(
                            {
                                "request_id": permission_id,
                                "tool": record["tool_name"],
                                "tool_name": record["tool_name"],
                                "operation_id": record["operation_id"],
                                "decision": decision,
                                "scope": scope,
                            }
                        ),
                        created_at=now,
                    )
                elif (
                    record["permission_status"] != permission_status
                    or record["permission_scope"] != scope
                    or str(record["decision_json"] or "") != decision_json
                ):
                    raise WaitDecisionConflictError(
                        "permission was already resolved with a different decision"
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(record["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "permission.resolve",
                    "command_id": command_id,
                    "permission_id": permission_id,
                    "run_id": run_id,
                    "resolved": True,
                    "decision": decision,
                    "scope": scope,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'permission.resolve', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        _now(),
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def request_plan_resolve_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        approval_id: str,
        decision: str,
        instructions: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "plan.resolve",
            "approval_id": approval_id,
            "decision": decision,
        }
        if instructions is not None:
            command_payload["instructions"] = instructions
        payload_json = _encode_payload(command_payload)
        status = {
            "approve": "approved",
            "modify": "modified",
            "reject": "rejected",
        }[decision]
        resume_decision = {
            "approval_id": approval_id,
            "decision": decision,
            "instructions": instructions,
        }
        decision_json = _encode_payload(resume_decision)

        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plan.resolve",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT p.run_id, p.wait_cycle_id, p.status AS approval_status, "
                        "p.instructions AS approval_instructions, "
                        "r.status AS run_status, r.principal_id, r.goal, r.user_input, r.workspace_path, "
                        "r.mode, r.history_json, r.settings_json, r.metadata_json, r.id AS id, "
                        "r.run_kind, r.root_run_id, r.agent_definition_id, "
                        "r.agent_definition_version, r.collaboration_depth, "
                        "r.collaboration_policy_json "
                        "FROM local_plan_approvals p JOIN local_runs r ON r.id = p.run_id "
                        "WHERE p.id = ? AND r.principal_id = ?",
                        (approval_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown plan approval: {approval_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)

                if record["approval_status"] == "pending":
                    if record["run_status"] not in {"waiting_permission", "waiting_input"}:
                        raise WaitDecisionConflictError(
                            "run is not awaiting a plan approval decision"
                        )
                    now = _now()
                    approval_cursor = await conn.execute(
                        "UPDATE local_plan_approvals SET status = ?, instructions = ?, "
                        "resolved_at = ? WHERE id = ? AND status = 'pending'",
                        (status, instructions, now, approval_id),
                    )
                    wait_cursor = await conn.execute(
                        "UPDATE local_wait_candidates SET status = 'resolved', "
                        "decision_json = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (decision_json, now, approval_id),
                    )
                    if approval_cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                        raise WaitDecisionConflictError("plan approval was resolved concurrently")
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "plan.approval_resolved",
                        payload_json=_encode_payload(
                            {
                                "request_id": approval_id,
                                "decision": decision,
                                "instructions": instructions,
                            }
                        ),
                        created_at=now,
                    )
                elif (
                    record["approval_status"] != status
                    or record["approval_instructions"] != instructions
                ):
                    raise WaitDecisionConflictError(
                        "plan approval was already resolved with a different decision"
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(record["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "plan.resolve",
                    "command_id": command_id,
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "resolved": True,
                    "decision": decision,
                    "instructions": instructions,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plan.resolve', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        _now(),
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def request_tool_reconcile_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        operation_id: str,
        decision: str,
        current_result_json: str | None,
        current_result_hash: str | None,
        prior_result_json: str,
        prior_result_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(
            {
                "type": "tool.reconcile",
                "operation_id": operation_id,
                "decision": decision,
            }
        )
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="tool.reconcile",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT c.*, r.status AS run_status, r.principal_id, r.goal, "
                        "r.user_input, r.workspace_path, r.mode, r.history_json, "
                        "r.settings_json, r.metadata_json, r.id AS id, r.run_kind, "
                        "r.root_run_id, r.agent_definition_id, r.agent_definition_version, "
                        "r.collaboration_depth, r.collaboration_policy_json "
                        "FROM local_wait_candidates c JOIN local_runs r ON r.id = c.run_id "
                        "WHERE c.id = ? AND c.kind = 'tool_reconciliation' "
                        "AND r.principal_id = ?",
                        (operation_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown tool reconciliation: {operation_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                if record["status"] == "pending" and record["run_status"] not in {
                    "waiting_permission",
                    "waiting_input",
                }:
                    raise WaitDecisionConflictError(
                        "run is not awaiting a tool reconciliation decision"
                    )

                updated, newly_resolved = await self._resolve_tool_reconciliation_uncommitted(
                    conn,
                    candidate_id=operation_id,
                    decision=decision,
                    current_result_json=current_result_json,
                    current_result_hash=current_result_hash,
                    prior_result_json=prior_result_json,
                    prior_result_hash=prior_result_hash,
                )
                now = _now()
                if newly_resolved:
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "tool.reconciliation_resolved",
                        payload_json=_encode_payload(
                            {
                                "request_id": operation_id,
                                "operation_id": operation_id,
                                "decision": decision,
                            }
                        ),
                        created_at=now,
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(updated["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "tool.reconcile",
                    "command_id": command_id,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "resolved": True,
                    "decision": decision,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'tool.reconcile', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def _request_run_cancel_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
    ) -> str | None:
        row = await (
            await conn.execute(
                "SELECT * FROM local_run_jobs WHERE run_id = ? "
                "AND status IN ('pending', 'leased') AND quarantined_at IS NULL",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            run = await (
                await conn.execute(
                    "SELECT status FROM local_runs WHERE id = ?",
                    (run_id,),
                )
            ).fetchone()
            if run is None or run["status"] not in {"waiting_permission", "waiting_input"}:
                return None
            requested_at = _now()
            await self._finish_run_cancel_uncommitted(
                conn,
                run_id=run_id,
                requested_at=requested_at,
            )
            return "waiting"
        job = dict(row)
        requested_at = _now()
        if job["status"] == "leased":
            await conn.execute(
                "UPDATE local_run_jobs SET cancel_requested_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased'",
                (requested_at, requested_at, job["id"]),
            )
            return "leased"

        await conn.execute(
            "UPDATE local_run_jobs SET status = 'canceled', cancel_requested_at = ?, "
            "updated_at = ?, finished_at = ? WHERE id = ? AND status = 'pending'",
            (requested_at, requested_at, requested_at, job["id"]),
        )
        await self._finish_run_cancel_uncommitted(
            conn,
            run_id=run_id,
            requested_at=requested_at,
        )
        return "pending"

    async def _finish_run_cancel_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        requested_at: str,
    ) -> None:
        cancel_decision = _encode_payload({"type": "cancel", "reason": "run_canceled"})
        await conn.execute(
            "UPDATE local_permissions SET status = 'canceled', decision_json = ?, "
            "resolved_at = ? WHERE run_id = ? AND status = 'pending'",
            (cancel_decision, requested_at, run_id),
        )
        await conn.execute(
            "UPDATE local_questions SET status = 'canceled' "
            "WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
        await conn.execute(
            "UPDATE local_wait_candidates SET status = 'resolved', decision_json = ?, "
            "resolved_at = ? WHERE run_id = ? AND status = 'pending'",
            (cancel_decision, requested_at, run_id),
        )
        await conn.execute(
            "UPDATE local_plan_approvals SET status = 'canceled', resolved_at = ? "
            "WHERE run_id = ? AND status = 'pending'",
            (requested_at, run_id),
        )
        await self._cancel_unstarted_tool_receipts_uncommitted(
            conn,
            run_id=run_id,
            canceled_at=requested_at,
        )
        await conn.execute(
            "UPDATE local_runs SET status = 'canceled', updated_at = ?, completed_at = ? "
            "WHERE id = ?",
            (requested_at, requested_at, run_id),
        )
        event = await self._append_event_uncommitted(
            conn,
            run_id,
            "run.canceled",
            payload_json="{}",
            created_at=requested_at,
        )
        await self._update_thread_projection_uncommitted(
            conn,
            run_id=run_id,
            run_status="canceled",
            change_type="run.canceled",
            payload={},
            event_high_watermark=int(event["seq"]),
            changed_at=requested_at,
        )

    async def bind_graph_definition(self, run_id: str, definition_id: str) -> None:
        """Bind once, then reject checkpoint execution with a different graph."""
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT graph_definition_id FROM local_runs WHERE id = ?", (run_id,)
                )
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            current = row[0]
            if current is not None and current != definition_id:
                raise GraphDefinitionMismatchError(
                    f"run {run_id} checkpoint is incompatible with the current agent definition"
                )
            if current is None:
                await conn.execute(
                    "UPDATE local_runs SET graph_definition_id = ?, updated_at = ? WHERE id = ?",
                    (definition_id, _now(), run_id),
                )

    async def advance_graph_checkpoint(
        self,
        run_id: str,
        *,
        graph_thread_id: str,
        expected_checkpoint_id: str | None,
        checkpoint_id: str,
    ) -> None:
        """Move one product Run's branch head with lease-fenced compare-and-swap."""
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT graph_thread_id, graph_checkpoint_id FROM local_runs WHERE id = ?",
                    (run_id,),
                )
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            if row[0] != graph_thread_id:
                raise GraphHeadConflictError(f"run {run_id} graph branch head changed")
            if row[1] == checkpoint_id:
                return
            if row[1] != expected_checkpoint_id:
                raise GraphHeadConflictError(f"run {run_id} graph branch head changed")
            if checkpoint_id != expected_checkpoint_id:
                await conn.execute(
                    "UPDATE local_runs SET graph_checkpoint_id = ?, updated_at = ? WHERE id = ?",
                    (checkpoint_id, _now(), run_id),
                )

    async def list_active_runs(self) -> list[dict[str, Any]]:
        """Runs not in a terminal state — used at boot to recover orphans left
        behind by a runtime restart (queued/running are dead and must be failed;
        waiting_permission/waiting_input are resumable from the checkpointer)."""
        cursor = await self._conn.execute(
            "SELECT * FROM local_runs WHERE status IN "
            "('queued', 'running', 'waiting_permission', 'waiting_input')"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT r.*, c.id AS command_id, c.client_message_id "
            "FROM local_runs r LEFT JOIN local_commands c ON c.run_id = r.id "
            "AND c.principal_id = r.principal_id "
            "AND c.command_type IN ('run.start', 'run.fork') "
            "WHERE r.id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_run_for_principal(
        self, *, principal_id: str, run_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT r.*, c.id AS command_id, c.client_message_id "
            "FROM local_runs r LEFT JOIN local_commands c ON c.run_id = r.id "
            "AND c.principal_id = r.principal_id "
            "AND c.command_type IN ('run.start', 'run.fork') "
            "WHERE r.principal_id = ? AND r.id = ?",
            (principal_id, run_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_runs(self, *, principal_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent runs newest-first with a per-row events_count.

        The client's `listLocalRuns()` (`client/src/runtime/
        client.ts:283`) reads `{runs: LocalRun[]}` on every boot to
        repopulate the conversation history sidebar. Each row must
        include the `LocalRun` fields the renderer reads:
        id, goal, status, workspace_path, created_at, updated_at,
        completed_at, canceled_at, events_count.
        """
        cursor = await self._conn.execute(
            """
            SELECT r.id, r.graph_thread_id, r.graph_checkpoint_id,
                   r.thread_id, r.assistant_item_id,
                   r.run_kind, r.root_run_id, r.agent_definition_id,
                   r.agent_definition_version, r.collaboration_depth,
                   r.collaboration_policy_json, r.spawn_operation_id,
                   r.goal, r.user_input, r.status, r.workspace_path,
                   r.created_at, r.updated_at, r.completed_at,
                   r.metadata_json, c.id AS command_id, c.client_message_id,
                   (SELECT COUNT(*) FROM local_events e
                      WHERE e.run_id = r.id) AS events_count
              FROM local_runs r
              LEFT JOIN local_commands c ON c.run_id = r.id
                   AND c.principal_id = r.principal_id
                   AND c.command_type IN ('run.start', 'run.fork')
             WHERE r.principal_id = ? AND r.run_kind <> 'child'
             ORDER BY datetime(r.updated_at) DESC, r.id DESC
             LIMIT ?
            """,
            (principal_id, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def list_threads(
        self,
        *,
        principal_id: str,
        limit: int = 100,
        before_created_at: str | None = None,
        before_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            page_limit = max(1, min(int(limit), 500))
            page_filter = ""
            params: list[Any] = [principal_id]
            if before_created_at is not None and before_id is not None:
                page_filter = "AND (created_at < ? OR (created_at = ? AND id < ?)) "
                params.extend([before_created_at, before_created_at, before_id])
            params.append(page_limit + 1)
            rows = await (
                await conn.execute(
                    "SELECT * FROM local_threads WHERE principal_id = ? AND deleted_at IS NULL "
                    + page_filter
                    + "ORDER BY created_at DESC, id DESC LIMIT ?",
                    params,
                )
            ).fetchall()
            cursor_row = await (
                await conn.execute(
                    "SELECT COALESCE(MAX(cursor), 0) FROM local_thread_changes WHERE principal_id = ?",
                    (principal_id,),
                )
            ).fetchone()
            await conn.commit()
            has_more = len(rows) > page_limit
            return (
                [dict(row) for row in rows[:page_limit]],
                int(cursor_row[0] if cursor_row else 0),
                has_more,
            )

    async def get_thread_snapshot(
        self,
        *,
        principal_id: str,
        thread_id: str,
        before_position: int | None = None,
        item_limit: int = 200,
        event_limit: int = 5000,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            thread = await (
                await conn.execute(
                    "SELECT * FROM local_threads WHERE principal_id = ? AND id = ? "
                    "AND deleted_at IS NULL",
                    (principal_id, thread_id),
                )
            ).fetchone()
            if thread is None:
                await conn.rollback()
                return None
            if expected_version is not None and int(thread["version"]) != expected_version:
                await conn.rollback()
                raise RunResultConflictError("thread changed while reading snapshot")
            bounded_item_limit = max(2, min(int(item_limit), 500))
            item_filter = ""
            item_params: list[Any] = [thread_id]
            if before_position is not None:
                item_filter = "AND position < ? "
                item_params.append(before_position)
            item_params.append(bounded_item_limit + 1)
            items = await (
                await conn.execute(
                    "SELECT * FROM local_thread_items WHERE thread_id = ? "
                    "AND superseded_at IS NULL "
                    + item_filter
                    + "ORDER BY position DESC, id DESC LIMIT ?",
                    item_params,
                )
            ).fetchall()
            has_more_items = len(items) > bounded_item_limit
            page_items = list(reversed(items[:bounded_item_limit]))
            run_ids = list(
                dict.fromkeys(str(item["run_id"]) for item in page_items if item["run_id"])
            )
            runs: list[aiosqlite.Row] = []
            subagent_invocations_by_run: dict[str, list[dict[str, Any]]] = {}
            tool_receipts_by_run: dict[str, list[dict[str, Any]]] = {}
            wait_candidates_by_run: dict[str, list[dict[str, Any]]] = {}
            artifacts_by_run: dict[str, list[dict[str, Any]]] = {}
            events: list[aiosqlite.Row] = []
            presentation_events: list[aiosqlite.Row] = []
            presentation_high_watermarks: dict[str, int] = {}
            event_high_watermarks: dict[str, int] = {}
            events_truncated = False
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                runs = await (
                    await conn.execute(
                        "SELECT r.*, c.id AS command_id, c.client_message_id, "
                        "(SELECT COUNT(*) FROM local_events e WHERE e.run_id = r.id) AS events_count "
                        "FROM local_runs r LEFT JOIN local_commands c ON c.run_id = r.id "
                        "AND c.principal_id = r.principal_id "
                        "AND c.command_type IN ('run.start', 'run.fork') "
                        f"WHERE r.principal_id = ? AND r.id IN ({placeholders}) "
                        "ORDER BY datetime(r.created_at), r.id",
                        [principal_id, *run_ids],
                    )
                ).fetchall()
                bounded_event_limit = max(1, min(int(event_limit), 10000))
                events = await (
                    await conn.execute(
                        "SELECT e.* FROM local_events e JOIN local_runs r ON r.id = e.run_id "
                        f"WHERE r.principal_id = ? AND e.run_id IN ({placeholders}) "
                        "ORDER BY datetime(r.created_at), r.id, e.seq LIMIT ?",
                        [principal_id, *run_ids, bounded_event_limit + 1],
                    )
                ).fetchall()
                events_truncated = len(events) > bounded_event_limit
                events = events[:bounded_event_limit]
                presentation_events = await (
                    await conn.execute(
                        "SELECT e.* FROM local_events e JOIN local_runs r ON r.id = e.run_id "
                        f"WHERE r.principal_id = ? AND e.run_id IN ({placeholders}) "
                        f"AND e.event_type IN ({','.join('?' for _ in _PRESENTATION_EVENT_TYPES)}) "
                        "ORDER BY datetime(r.created_at), r.id, e.seq",
                        [principal_id, *run_ids, *_PRESENTATION_EVENT_TYPES],
                    )
                ).fetchall()
                subagent_invocations = await self._subagent_invocations_uncommitted(
                    conn,
                    run_ids,
                )
                for invocation in subagent_invocations:
                    subagent_invocations_by_run.setdefault(
                        str(invocation["parent_run_id"]), []
                    ).append(invocation)
                tool_receipts = await (
                    await conn.execute(
                        "SELECT * FROM local_tool_receipts "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, operation_id",
                        run_ids,
                    )
                ).fetchall()
                for receipt in tool_receipts:
                    tool_receipts_by_run.setdefault(str(receipt["run_id"]), []).append(
                        dict(receipt)
                    )
                wait_candidates = await (
                    await conn.execute(
                        "SELECT * FROM local_wait_candidates "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()
                for candidate in wait_candidates:
                    wait_candidates_by_run.setdefault(str(candidate["run_id"]), []).append(
                        dict(candidate)
                    )
                artifacts = await (
                    await conn.execute(
                        "SELECT * FROM local_artifacts "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()
                for artifact in artifacts:
                    artifacts_by_run.setdefault(str(artifact["run_id"]), []).append(dict(artifact))
                event_high_watermarks = dict.fromkeys(run_ids, 0)
                presentation_high_watermarks = dict.fromkeys(run_ids, 0)
                for event in events:
                    event_run_id = str(event["run_id"])
                    event_high_watermarks[event_run_id] = max(
                        event_high_watermarks[event_run_id],
                        int(event["seq"]),
                    )
                for event in presentation_events:
                    event_run_id = str(event["run_id"])
                    presentation_high_watermarks[event_run_id] = int(event["seq"])
            cursor_row = await (
                await conn.execute(
                    "SELECT COALESCE(MAX(cursor), 0) FROM local_thread_changes "
                    "WHERE principal_id = ? AND thread_id = ?",
                    (principal_id, thread_id),
                )
            ).fetchone()
            await conn.commit()
            return {
                "thread": dict(thread),
                "items": [dict(item) for item in page_items],
                "runs": [
                    {
                        **dict(run),
                        "subagent_invocations": subagent_invocations_by_run.get(str(run["id"]), []),
                    }
                    for run in runs
                ],
                "events": [dict(event) for event in events],
                "presentation_events": [dict(event) for event in presentation_events],
                "presentation_high_watermarks": presentation_high_watermarks,
                "tool_receipts_by_run": tool_receipts_by_run,
                "wait_candidates_by_run": wait_candidates_by_run,
                "artifacts_by_run": artifacts_by_run,
                "event_high_watermarks": event_high_watermarks,
                "cursor": int(cursor_row[0] if cursor_row else 0),
                "has_more_items": has_more_items,
                "next_before_position": int(page_items[0]["position"])
                if has_more_items and page_items
                else None,
                "events_truncated": events_truncated,
            }

    async def get_run_presentation_facts(self, run_id: str) -> dict[str, Any] | None:
        """Read one Run's presentation sources from a single SQLite snapshot."""
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            run = await (
                await conn.execute("SELECT * FROM local_runs WHERE id = ?", (run_id,))
            ).fetchone()
            if run is None:
                await conn.rollback()
                return None
            assistant_item = None
            if run["assistant_item_id"]:
                assistant_item = await (
                    await conn.execute(
                        "SELECT * FROM local_thread_items WHERE id = ? AND run_id = ?",
                        (run["assistant_item_id"], run_id),
                    )
                ).fetchone()
            events = await (
                await conn.execute(
                    "SELECT * FROM local_events WHERE run_id = ? "
                    f"AND event_type IN ({','.join('?' for _ in _PRESENTATION_EVENT_TYPES)}) "
                    "ORDER BY seq",
                    (run_id, *_PRESENTATION_EVENT_TYPES),
                )
            ).fetchall()
            receipts = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                    "ORDER BY created_at, operation_id",
                    (run_id,),
                )
            ).fetchall()
            wait_candidates = await (
                await conn.execute(
                    "SELECT * FROM local_wait_candidates WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                )
            ).fetchall()
            artifacts = await (
                await conn.execute(
                    "SELECT * FROM local_artifacts WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                )
            ).fetchall()
            await conn.commit()
            return {
                "run": dict(run),
                "assistant_item": dict(assistant_item) if assistant_item is not None else None,
                "events": [dict(event) for event in events],
                "tool_receipts": [dict(receipt) for receipt in receipts],
                "wait_candidates": [dict(candidate) for candidate in wait_candidates],
                "artifacts": [dict(artifact) for artifact in artifacts],
                "event_high_watermark": int(events[-1]["seq"]) if events else 0,
            }

    async def update_thread(
        self,
        *,
        principal_id: str,
        thread_id: str,
        title: str | None,
        metadata: dict[str, Any] | None,
        archived: bool | None,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT * FROM local_threads WHERE principal_id = ? AND id = ? "
                        "AND deleted_at IS NULL",
                        (principal_id, thread_id),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                now = _now()
                version = int(row["version"]) + 1
                archived_at = row["archived_at"]
                if archived is True and archived_at is None:
                    archived_at = now
                elif archived is False:
                    archived_at = None
                await conn.execute(
                    "UPDATE local_threads SET title = ?, metadata_json = ?, archived_at = ?, "
                    "version = ?, updated_at = ? WHERE id = ?",
                    (
                        " ".join((title or row["title"]).split())[:80],
                        _encode_payload(metadata) if metadata is not None else row["metadata_json"],
                        archived_at,
                        version,
                        now,
                        thread_id,
                    ),
                )
                await conn.execute(
                    "INSERT INTO local_thread_changes "
                    "(principal_id, thread_id, thread_version, change_type, created_at) "
                    "VALUES (?, ?, ?, 'thread.updated', ?)",
                    (principal_id, thread_id, version, now),
                )
                updated = await (
                    await conn.execute("SELECT * FROM local_threads WHERE id = ?", (thread_id,))
                ).fetchone()
                await conn.commit()
                return dict(updated) if updated else None
            except BaseException:
                await conn.rollback()
                raise

    async def delete_thread(self, *, principal_id: str, thread_id: str) -> int | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT * FROM local_threads WHERE principal_id = ? AND id = ?",
                        (principal_id, thread_id),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                if row["deleted_at"] is not None:
                    await conn.commit()
                    return int(row["version"])
                active = await (
                    await conn.execute(
                        "SELECT 1 FROM local_runs r "
                        "LEFT JOIN local_run_jobs j ON j.run_id = r.id "
                        "WHERE r.principal_id = ? AND r.thread_id = ? AND ("
                        "r.status NOT IN ('completed', 'failed', 'canceled') OR "
                        "j.status IN ('pending', 'leased')) LIMIT 1",
                        (principal_id, thread_id),
                    )
                ).fetchone()
                if active is not None:
                    raise RunResultConflictError("thread has an unsettled run")
                now = _now()
                version = int(row["version"]) + 1
                await conn.execute(
                    "UPDATE local_threads SET deleted_at = ?, archived_at = ?, version = ?, "
                    "updated_at = ? WHERE id = ?",
                    (now, now, version, now, thread_id),
                )
                await conn.execute(
                    "INSERT INTO local_thread_changes "
                    "(principal_id, thread_id, thread_version, change_type, created_at) "
                    "VALUES (?, ?, ?, 'thread.deleted', ?)",
                    (principal_id, thread_id, version, now),
                )
                await conn.commit()
                return version
            except BaseException:
                await conn.rollback()
                raise

    async def thread_changes_since(
        self,
        *,
        principal_id: str,
        after_cursor: int,
        limit: int = 500,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = await (
            await self._conn.execute(
                "SELECT cursor, thread_id, thread_version, change_type, run_id, created_at "
                "FROM local_thread_changes WHERE principal_id = ? AND cursor > ? "
                "ORDER BY cursor LIMIT ?",
                (principal_id, max(0, int(after_cursor)), max(1, min(int(limit), 1000))),
            )
        ).fetchall()
        changes = [dict(row) for row in rows]
        return changes, (int(changes[-1]["cursor"]) if changes else max(0, int(after_cursor)))

    async def list_scheduled_runs_for_principal(
        self,
        *,
        principal_id: str,
        limit: int = 50,
        status: str | None = None,
        notify_pending: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["principal_id = ?"]
        params: list[Any] = [principal_id]
        if notify_pending:
            clauses.extend(["status IN ('completed', 'failed')", "notified_at IS NULL"])
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if notify_pending:
            order = "datetime(updated_at) ASC, id ASC"
        elif status:
            order = "datetime(run_at) ASC, id ASC"
        else:
            order = "datetime(run_at) DESC, id DESC"
        params.append(limit)
        cursor = await self._conn.execute(
            f"SELECT * FROM local_scheduled_runs WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order} LIMIT ?",
            tuple(params),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_run_status(
        self, run_id: str, status: str, *, completed_at: str | None = None
    ) -> None:
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "UPDATE local_runs SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (status, _now(), completed_at, run_id),
            )

    async def commit_run_result(
        self,
        run_id: str,
        *,
        status: str,
        event_type: str,
        payload: dict[str, Any],
        orphan_recovery: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically persist a waiting or terminal run state and its event.

        Returns ``(event, created)``. Repeating the exact same result returns
        the original event with ``created=False``; a different result cannot
        replace an already persisted waiting/terminal result.
        """
        expected_event = _RUN_RESULT_EVENTS.get(status)
        if expected_event != event_type:
            raise ValueError(f"{status!r} must be committed with {expected_event!r}")

        payload_json = _encode_payload(payload)
        execution_lease = _CURRENT_EXECUTION_LEASE.get()
        async with self.run_write_transaction(run_id) as conn:
            run_row = await (
                await conn.execute("SELECT status FROM local_runs WHERE id = ?", (run_id,))
            ).fetchone()
            if run_row is None:
                raise KeyError(f"unknown run: {run_id}")

            if execution_lease is None:
                if not orphan_recovery:
                    raise LeaseFenceError(
                        f"run {run_id} result requires the active execution lease"
                    )
                active_job = await (
                    await conn.execute(
                        "SELECT 1 FROM local_run_jobs WHERE run_id = ? "
                        "AND status IN ('pending', 'leased') LIMIT 1",
                        (run_id,),
                    )
                ).fetchone()
                if active_job is not None:
                    raise LeaseFenceError(f"run {run_id} still has an unsettled execution job")

            current_status = str(run_row[0])
            if current_status == status:
                existing_row = await (
                    await conn.execute(
                        "SELECT * FROM local_events WHERE run_id = ? AND event_type = ? "
                        "ORDER BY seq DESC LIMIT 1",
                        (run_id, event_type),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = dict(existing_row)
                    try:
                        existing_payload = _encode_payload(
                            json.loads(existing["payload_json"] or "{}")
                        )
                    except (json.JSONDecodeError, TypeError):
                        existing_payload = str(existing["payload_json"])
                    if existing_payload == payload_json:
                        if execution_lease is not None:
                            await self._settle_execution_job_uncommitted(
                                conn,
                                execution_lease,
                                run_status=status,
                                settled_at=_now(),
                            )
                        return existing, False
                    raise RunResultConflictError(
                        f"run {run_id} already has a different {status} result"
                    )
            elif current_status in _TERMINAL_RUN_STATUSES:
                raise RunResultConflictError(
                    f"run {run_id} is already terminal with status {current_status}"
                )

            committed_at = _now()
            if status in _TERMINAL_RUN_STATUSES:
                await self._cancel_unstarted_tool_receipts_uncommitted(
                    conn,
                    run_id=run_id,
                    canceled_at=committed_at,
                    error_type={
                        "completed": "ParentRunCompleted",
                        "failed": "ParentRunFailed",
                        "canceled": "RunCanceled",
                    }[status],
                )
            completed_at = committed_at if status in _TERMINAL_RUN_STATUSES else None
            await conn.execute(
                "UPDATE local_runs SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (status, committed_at, completed_at, run_id),
            )
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                event_type,
                payload_json=payload_json,
                created_at=committed_at,
            )
            await self._update_thread_projection_uncommitted(
                conn,
                run_id=run_id,
                run_status=status,
                change_type=event_type,
                payload=payload,
                event_high_watermark=int(event["seq"]),
                changed_at=committed_at,
            )
            if execution_lease is not None:
                await self._settle_execution_job_uncommitted(
                    conn,
                    execution_lease,
                    run_status=status,
                    settled_at=committed_at,
                )
            return event, True

    @staticmethod
    async def _update_thread_projection_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        run_status: str,
        change_type: str,
        payload: dict[str, Any],
        event_high_watermark: int,
        changed_at: str,
    ) -> None:
        run = await (
            await conn.execute(
                "SELECT principal_id, thread_id, assistant_item_id FROM local_runs WHERE id = ?",
                (run_id,),
            )
        ).fetchone()
        if run is None or not run["thread_id"] or not run["assistant_item_id"]:
            return
        draft = await (
            await conn.execute(
                "SELECT content FROM local_assistant_drafts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        content = str(payload.get("final_text") or "") if run_status == "completed" else ""
        if not content and draft is not None:
            content = str(draft["content"] or "")
        item_status = {
            "completed": "completed",
            "failed": "failed",
            "canceled": "canceled",
            "waiting_permission": "in_progress",
            "waiting_input": "in_progress",
            "cleanup_required": "cleanup_required",
        }[run_status]
        terminal = run_status in _TERMINAL_RUN_STATUSES
        cursor = await conn.execute(
            "UPDATE local_thread_items SET status = ?, content = ?, event_high_watermark = ?, "
            "version = version + 1, updated_at = ?, completed_at = ? "
            "WHERE id = ? AND run_id = ?",
            (
                item_status,
                content,
                event_high_watermark,
                changed_at,
                changed_at if terminal else None,
                run["assistant_item_id"],
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RunResultConflictError(f"run {run_id} is missing its assistant projection")
        thread = await (
            await conn.execute(
                "SELECT version, title FROM local_threads WHERE id = ? AND principal_id = ?",
                (run["thread_id"], run["principal_id"]),
            )
        ).fetchone()
        if thread is None:
            raise RunResultConflictError(f"run {run_id} is missing its thread projection")
        thread_version = int(thread["version"]) + 1
        generated_title = " ".join(str(payload.get("thread_title") or "").split())[:80]
        title_seed = " ".join(str(payload.get("thread_title_seed") or "").split())[:80]
        next_title = (
            generated_title
            if generated_title and title_seed and str(thread["title"]) == title_seed
            else str(thread["title"])
        )
        await conn.execute(
            "UPDATE local_threads SET version = ?, title = ?, updated_at = ? WHERE id = ?",
            (thread_version, next_title, changed_at, run["thread_id"]),
        )
        await conn.execute(
            "INSERT INTO local_thread_changes "
            "(principal_id, thread_id, thread_version, change_type, run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run["principal_id"],
                run["thread_id"],
                thread_version,
                change_type,
                run_id,
                changed_at,
            ),
        )

    @staticmethod
    async def _settle_execution_job_uncommitted(
        conn: aiosqlite.Connection,
        lease: ExecutionLease,
        *,
        run_status: str,
        settled_at: str,
    ) -> None:
        fence_at = _now()
        job_status = {
            "completed": "completed",
            "failed": "dead",
            "canceled": "canceled",
            "waiting_permission": "completed",
            "waiting_input": "completed",
        }[run_status]
        cursor = await conn.execute(
            "UPDATE local_run_jobs SET status = ?, updated_at = ?, finished_at = ?, "
            "lease_expires_at = NULL WHERE id = ? AND run_id = ? AND status = 'leased' "
            "AND lease_owner = ? AND lease_generation = ? AND quarantined_at IS NULL "
            "AND lease_expires_at > ?",
            (
                job_status,
                settled_at,
                settled_at,
                lease.job_id,
                lease.run_id,
                lease.lease_owner,
                lease.lease_generation,
                fence_at,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseFenceError(
                f"run {lease.run_id} lease generation {lease.lease_generation} is stale"
            )

    # --- scheduled runs ---

    async def create_scheduled_run(
        self,
        *,
        principal_id: str,
        goal: str,
        run_at: str,
        workspace_path: str | None = None,
        model: str = "auto",
        history: list[dict[str, str]] | None = None,
        settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": _new_id("sched"),
            "principal_id": principal_id,
            "goal": goal,
            "workspace_path": workspace_path,
            "model": model or "auto",
            "history_json": json.dumps(history or [], ensure_ascii=False, default=str),
            "settings_json": json.dumps(settings or {}, ensure_ascii=False, default=str),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, default=str),
            "run_at": run_at,
            "status": "scheduled",
            "run_id": None,
            "result_text": None,
            "error_message": None,
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
            "notified_at": None,
        }
        path_error = await self._workspace_path_error(workspace_path)
        if path_error is not None:
            raise WorkspaceAdmissionError(path_error)
        async with aiosqlite.connect(str(self._db_path)) as transaction_conn:
            await _configure_connection(transaction_conn)
            await transaction_conn.execute("BEGIN IMMEDIATE")
            try:
                workspace_error = await self._workspace_owner_error(
                    transaction_conn,
                    principal_id=principal_id,
                    path=workspace_path,
                )
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                await transaction_conn.execute(
                    "INSERT INTO local_scheduled_runs "
                    "(id, principal_id, goal, workspace_path, model, history_json, "
                    " settings_json, metadata_json, run_at, status, run_id, result_text, "
                    " error_message, created_at, updated_at, completed_at, notified_at) "
                    "VALUES (:id, :principal_id, :goal, :workspace_path, :model, "
                    " :history_json, :settings_json, :metadata_json, :run_at, :status, "
                    " :run_id, :result_text, :error_message, :created_at, :updated_at, "
                    " :completed_at, :notified_at)",
                    record,
                )
                await transaction_conn.commit()
            except BaseException:
                await transaction_conn.rollback()
                raise
        return record

    async def get_scheduled_run(self, schedule_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_scheduled_runs WHERE id = ?",
            (schedule_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_scheduled_runs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        notify_pending: bool = False,
    ) -> list[dict[str, Any]]:
        if notify_pending:
            cursor = await self._conn.execute(
                """
                SELECT * FROM local_scheduled_runs
                 WHERE status IN ('completed', 'failed')
                   AND notified_at IS NULL
                 ORDER BY datetime(updated_at) ASC, id ASC
                 LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]
        if status:
            cursor = await self._conn.execute(
                """
                SELECT * FROM local_scheduled_runs
                 WHERE status = ?
                 ORDER BY datetime(run_at) ASC, id ASC
                 LIMIT ?
                """,
                (status, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]
        cursor = await self._conn.execute(
            """
            SELECT * FROM local_scheduled_runs
             ORDER BY datetime(run_at) DESC, id DESC
             LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def claim_due_scheduled_runs(
        self,
        *,
        now: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                rows = await (
                    await conn.execute(
                        "SELECT * FROM local_scheduled_runs "
                        "WHERE status = 'scheduled' AND run_at <= ? "
                        "ORDER BY run_at ASC, id ASC LIMIT ?",
                        (now, limit),
                    )
                ).fetchall()
                if not rows:
                    await conn.commit()
                    return []
                updated_at = _now()
                await conn.executemany(
                    "UPDATE local_scheduled_runs SET status = 'running', updated_at = ? "
                    "WHERE id = ? AND status = 'scheduled'",
                    [(updated_at, row["id"]) for row in rows],
                )
                await conn.commit()
                return [
                    {**dict(row), "status": "running", "updated_at": updated_at} for row in rows
                ]
            except BaseException:
                await conn.rollback()
                raise

    async def mark_scheduled_run_started(
        self, schedule_id: str, run_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs SET status = 'running', run_id = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running' AND (run_id IS NULL OR run_id = ?) "
            "RETURNING *",
            (run_id, _now(), schedule_id, run_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self.get_scheduled_run(schedule_id)

    async def complete_scheduled_run(
        self,
        schedule_id: str,
        *,
        status: str,
        result_text: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"completed", "failed", "canceled"}:
            raise ValueError(f"invalid scheduled run terminal status: {status}")
        completed_at = _now()
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs "
            "SET status = ?, result_text = ?, error_message = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running' RETURNING *",
            (status, result_text, error_message, completed_at, completed_at, schedule_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self.get_scheduled_run(schedule_id)

    async def cancel_scheduled_run(
        self, *, principal_id: str, schedule_id: str
    ) -> dict[str, Any] | None:
        canceled_at = _now()
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs "
            "SET status = 'canceled', completed_at = ?, updated_at = ? "
            "WHERE principal_id = ? AND id = ? AND status = 'scheduled' RETURNING *",
            (canceled_at, canceled_at, principal_id, schedule_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self._scheduled_run_for_principal(principal_id, schedule_id)

    async def mark_scheduled_run_notified(
        self, *, principal_id: str, schedule_id: str
    ) -> dict[str, Any] | None:
        notified_at = _now()
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs SET notified_at = ?, updated_at = ? "
            "WHERE principal_id = ? AND id = ? "
            "AND status IN ('completed', 'failed') AND notified_at IS NULL RETURNING *",
            (notified_at, notified_at, principal_id, schedule_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self._scheduled_run_for_principal(principal_id, schedule_id)

    async def _scheduled_run_for_principal(
        self, principal_id: str, schedule_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_scheduled_runs WHERE principal_id = ? AND id = ?",
            (principal_id, schedule_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- events ---

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with self.run_write_transaction(run_id) as conn:
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                event_type,
                payload_json=_encode_payload(payload),
                created_at=_now(),
            )
            return event

    async def _append_event_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        event_type: str,
        *,
        payload_json: str,
        created_at: str,
        project_child: bool = True,
    ) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM local_events WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        next_seq = (row[0] if row else 0) + 1
        event = {
            "id": _new_id("evt"),
            "run_id": run_id,
            "seq": next_seq,
            "event_type": event_type,
            "payload_json": payload_json,
            "created_at": created_at,
        }
        await conn.execute(
            "INSERT INTO local_events (id, run_id, seq, event_type, payload_json, created_at) "
            "VALUES (:id, :run_id, :seq, :event_type, :payload_json, :created_at)",
            event,
        )
        if project_child and event_type in _CHILD_EVENT_BY_RUN_EVENT:
            parent_event = await self._append_child_parent_lifecycle_uncommitted(
                conn,
                child_run_id=run_id,
                source_event=event,
                source_payload=_json_payload(payload_json),
            )
            if parent_event is not None:
                event["_parent_event"] = parent_event
        return event

    async def _append_child_parent_lifecycle_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        child_run_id: str,
        source_event: dict[str, Any],
        source_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        child = await (
            await conn.execute(
                "SELECT id, parent_run_id, root_run_id, agent_definition_id, "
                "agent_definition_version, collaboration_depth, goal, status, updated_at "
                "FROM local_runs WHERE id = ? AND run_kind = 'child'",
                (child_run_id,),
            )
        ).fetchone()
        if child is None or not child["parent_run_id"]:
            return None
        parent_event_type, fixed_status = _CHILD_EVENT_BY_RUN_EVENT[str(source_event["event_type"])]
        status = fixed_status or str(child["status"])
        final_text = str(source_payload.get("final_text") or "")
        payload: dict[str, Any] = {
            "child_run_id": str(child["id"]),
            "parent_run_id": str(child["parent_run_id"]),
            "root_run_id": str(child["root_run_id"]),
            "agent_definition_id": str(child["agent_definition_id"]),
            "agent_definition_version": str(child["agent_definition_version"]),
            "collaboration_depth": int(child["collaboration_depth"]),
            "goal": str(child["goal"]),
            "status": status,
            "source_event_id": str(source_event["id"]),
            "source_event_seq": int(source_event["seq"]),
            "updated_at": str(child["updated_at"]),
        }
        if final_text:
            payload["result_preview"] = final_text[:4096]
            payload["result_truncated"] = len(final_text) > 4096
        for key in (
            "error",
            "type",
            "category",
            "retryable",
            "input_tokens",
            "output_tokens",
            "model_calls",
            "final_answer_ref",
        ):
            if key in source_payload:
                payload[key] = source_payload[key]
        parent_event = await self._append_event_uncommitted(
            conn,
            str(child["parent_run_id"]),
            parent_event_type,
            payload_json=_encode_payload(payload),
            created_at=str(source_event["created_at"]),
            project_child=False,
        )
        await self._touch_thread_for_run_event_uncommitted(
            conn,
            run_id=str(child["parent_run_id"]),
            change_type=parent_event_type,
            event_high_watermark=int(parent_event["seq"]),
            changed_at=str(source_event["created_at"]),
        )
        return parent_event

    async def events_since(
        self,
        run_id: str,
        after_seq: int = 0,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM local_events WHERE run_id = ? AND seq > ? ORDER BY seq"
        parameters: tuple[Any, ...] = (run_id, after_seq)
        if limit is not None:
            sql += " LIMIT ?"
            parameters += (limit,)
        cursor = await self._conn.execute(sql, parameters)
        return [dict(row) for row in await cursor.fetchall()]

    async def event_sequence_window(self, run_id: str) -> tuple[int | None, int]:
        row = await (
            await self._conn.execute(
                "SELECT MIN(seq), COALESCE(MAX(seq), 0) FROM local_events WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return (int(row[0]) if row and row[0] is not None else None, int(row[1]) if row else 0)

    # --- steering ---

    async def create_steering_instruction(
        self,
        *,
        run_id: str,
        content: str,
    ) -> dict[str, Any]:
        record = {
            "id": _new_id("steer"),
            "run_id": run_id,
            "content": content,
            "status": "pending",
            "created_at": _now(),
            "injected_at": None,
        }
        await self._conn.execute(
            "INSERT INTO local_steering "
            "(id, run_id, content, status, created_at, injected_at) "
            "VALUES (:id, :run_id, :content, :status, :created_at, :injected_at)",
            record,
        )
        return record

    async def request_run_inject_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        run_id: str,
        content: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload({"type": "run.inject", "run_id": run_id, "content": content})
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="run.inject",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                run = await (
                    await conn.execute(
                        "SELECT id, status FROM local_runs WHERE principal_id = ? AND id = ?",
                        (principal_id, run_id),
                    )
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown run: {run_id}")
                if str(run["status"]) in _TERMINAL_RUN_STATUSES | {"cleanup_required"}:
                    raise RunAdmissionError("run_not_active", "run is not active")

                now = _now()
                instruction_id = _new_id("steer")
                await conn.execute(
                    "INSERT INTO local_steering "
                    "(id, run_id, content, status, created_at, injected_at) "
                    "VALUES (?, ?, ?, 'pending', ?, NULL)",
                    (instruction_id, run_id, content, now),
                )
                receipt = {
                    "command_id": command_id,
                    "run_id": run_id,
                    "instruction_id": instruction_id,
                    "queued": True,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'run.inject', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def claim_pending_steering(self, run_id: str) -> list[dict[str, Any]]:
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "SELECT * FROM local_steering "
                "WHERE run_id = ? AND status = 'pending' ORDER BY created_at, id",
                (run_id,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            if not rows:
                return []
            injected_at = _now()
            await conn.executemany(
                "UPDATE local_steering SET status = 'injected', injected_at = ? "
                "WHERE id = ? AND status = 'pending'",
                [(injected_at, row["id"]) for row in rows],
            )
            return [{**row, "status": "injected", "injected_at": injected_at} for row in rows]

    # --- plan approvals ---

    async def create_plan_approval(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        todos: list[dict[str, Any]],
        summary: str = "",
        wait_cycle_id: str | None = None,
        interrupt_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": _new_id("plan"),
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "todos_json": json.dumps(todos, ensure_ascii=False, default=str),
            "summary": summary,
            "status": "pending",
            "instructions": None,
            "created_at": _now(),
            "resolved_at": None,
        }
        record["wait_cycle_id"] = record["wait_cycle_id"] or record["id"]
        record["interrupt_id"] = record["interrupt_id"] or record["id"]
        try:
            async with self.run_write_transaction(run_id) as conn:
                await conn.execute(
                    "INSERT INTO local_plan_approvals "
                    "(id, run_id, tool_call_id, wait_cycle_id, interrupt_id, todos_json, "
                    "summary, status, instructions, created_at, resolved_at) "
                    "VALUES (:id, :run_id, :tool_call_id, :wait_cycle_id, :interrupt_id, "
                    ":todos_json, :summary, :status, :instructions, :created_at, :resolved_at)",
                    record,
                )
                await conn.execute(
                    "INSERT INTO local_wait_candidates "
                    "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                    "payload_json, decision_json, created_at, resolved_at) "
                    "VALUES (?, ?, 'plan', ?, ?, 0, 'pending', ?, NULL, ?, NULL)",
                    (
                        record["id"],
                        run_id,
                        record["wait_cycle_id"],
                        record["interrupt_id"],
                        record["todos_json"],
                        record["created_at"],
                    ),
                )
        except aiosqlite.IntegrityError:
            existing = await self.get_plan_approval_by_tool_call(
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
            assert existing is not None
            if (
                wait_cycle_id
                and interrupt_id
                and (not existing.get("wait_cycle_id") or not existing.get("interrupt_id"))
            ):
                async with self.run_write_transaction(run_id) as conn:
                    await conn.execute(
                        "UPDATE local_plan_approvals SET wait_cycle_id = ?, interrupt_id = ? "
                        "WHERE id = ?",
                        (wait_cycle_id, interrupt_id, existing["id"]),
                    )
                    await conn.execute(
                        "INSERT OR IGNORE INTO local_wait_candidates "
                        "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                        "payload_json, decision_json, created_at, resolved_at) "
                        "VALUES (?, ?, 'plan', ?, ?, 0, 'pending', ?, NULL, ?, NULL)",
                        (
                            existing["id"],
                            run_id,
                            wait_cycle_id,
                            interrupt_id,
                            existing["todos_json"],
                            existing["created_at"],
                        ),
                    )
                existing = {
                    **existing,
                    "wait_cycle_id": wait_cycle_id,
                    "interrupt_id": interrupt_id,
                }
            return existing
        return _decode_plan_approval_record(record)

    async def get_plan_approval(self, approval_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_plan_approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        return _decode_plan_approval_record(dict(row)) if row else None

    async def get_plan_approval_by_tool_call(
        self,
        *,
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_plan_approvals WHERE run_id = ? AND tool_call_id = ?",
            (run_id, tool_call_id),
        )
        row = await cursor.fetchone()
        return _decode_plan_approval_record(dict(row)) if row else None

    # --- permissions (HumanInTheLoop pause record) ---

    async def create_permission(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_version: str = "",
        operation_id: str | None = None,
        arguments_hash: str | None = None,
        risk: str | None = None,
        wait_cycle_id: str | None = None,
        interrupt_id: str | None = None,
        action_index: int = 0,
        scope: str = "once",
    ) -> dict[str, Any]:
        """Get or create the approval record for one concrete tool operation.

        The returned `id` is what gets surfaced to the renderer as the
        `request_id` in the `permission.required` SSE event, and what
        the client posts back to `POST /v1/permissions/{id}` to
        approve or deny. Without this row, the client cannot look up
        which paused run to resume.
        """
        if operation_id:
            cursor = await self._conn.execute(
                "SELECT * FROM local_permissions WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            )
            row = await cursor.fetchone()
            if row is not None:
                existing = dict(row)
                if (
                    existing.get("tool_call_id") != tool_call_id
                    or existing.get("tool_name") != tool_name
                    or str(existing.get("tool_version") or "") != tool_version
                    or existing.get("arguments_hash") != arguments_hash
                ):
                    raise PermissionDecisionConflictError(
                        "permission operation identity was reused with different content"
                    )
                return existing

        record = {
            "id": _new_id("perm"),
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "action_index": max(0, int(action_index)),
            "operation_id": operation_id,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "arguments_hash": arguments_hash,
            "arguments_json": json.dumps(arguments or {}, ensure_ascii=False, default=str),
            "risk": risk,
            "decision_json": None,
            "status": "pending",
            "scope": scope,
            "grant_max_uses": 0,
            "grant_use_count": 0,
            "grant_expires_at": None,
            "created_at": _now(),
            "resolved_at": None,
        }
        record["wait_cycle_id"] = record["wait_cycle_id"] or record["id"]
        record["interrupt_id"] = record["interrupt_id"] or record["id"]
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "INSERT INTO local_permissions "
                "(id, run_id, tool_call_id, wait_cycle_id, interrupt_id, action_index, "
                "operation_id, tool_name, tool_version, arguments_hash, "
                " arguments_json, risk, decision_json, status, scope, grant_max_uses, "
                " grant_use_count, grant_expires_at, created_at, resolved_at) "
                "VALUES (:id, :run_id, :tool_call_id, :wait_cycle_id, :interrupt_id, "
                ":action_index, :operation_id, :tool_name, :tool_version, "
                "        :arguments_hash, :arguments_json, :risk, :decision_json, :status, :scope, "
                "        :grant_max_uses, :grant_use_count, :grant_expires_at, "
                "        :created_at, :resolved_at)",
                record,
            )
            await conn.execute(
                "INSERT INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (?, ?, 'tool_review', ?, ?, ?, 'pending', ?, NULL, ?, NULL)",
                (
                    record["id"],
                    run_id,
                    record["wait_cycle_id"],
                    record["interrupt_id"],
                    record["action_index"],
                    json.dumps(
                        {
                            "tool_call_id": tool_call_id,
                            "operation_id": operation_id,
                            "tool_name": tool_name,
                            "tool_version": tool_version,
                            "arguments_hash": arguments_hash,
                            "arguments": arguments or {},
                            "risk": risk,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    record["created_at"],
                ),
            )
        return record

    async def get_permission(self, permission_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_permissions WHERE id = ?", (permission_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_permission_for_operation(
        self, *, run_id: str, operation_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_permissions WHERE run_id = ? AND operation_id = ?",
            (run_id, operation_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def resolve_permission(
        self,
        permission_id: str,
        *,
        status: str,
        scope: str | None = None,
        decision: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"approved", "denied"}:
            raise ValueError("permission status must be approved or denied")
        record = await self.get_permission(permission_id)
        if record is None:
            return None
        current_status = str(record.get("status") or "")
        current_scope = str(record.get("scope") or "once")
        requested_scope = scope or current_scope
        require_allowed_permission_scope(
            tool_name=str(record["tool_name"]),
            risk=record.get("risk"),
            status=status,
            scope=requested_scope,
        )
        grant_max_uses = -1 if requested_scope == "run" and status == "approved" else 0
        grant_expires_at = None
        decision_json = json.dumps(
            decision or ({"type": "approve"} if status == "approved" else {"type": "reject"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if current_status != "pending":
            if (
                current_status != status
                or current_scope != requested_scope
                or str(record.get("decision_json") or "") != decision_json
            ):
                raise PermissionDecisionConflictError(
                    "permission was already resolved with a different decision"
                )
            return record
        async with self.run_write_transaction(str(record["run_id"])) as conn:
            run = await (
                await conn.execute(
                    "SELECT status FROM local_runs WHERE id = ?",
                    (record["run_id"],),
                )
            ).fetchone()
            if run is None or run["status"] in {
                "completed",
                "failed",
                "canceled",
                "cleanup_required",
            }:
                raise WaitDecisionConflictError("run is not awaiting a decision")
            cursor = await conn.execute(
                "UPDATE local_permissions SET status = ?, scope = ?, decision_json = ?, "
                "grant_max_uses = ?, grant_expires_at = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (
                    status,
                    requested_scope,
                    decision_json,
                    grant_max_uses,
                    grant_expires_at,
                    _now(),
                    permission_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionDecisionConflictError("permission was resolved concurrently")
            wait_cursor = await conn.execute(
                "UPDATE local_wait_candidates SET status = 'resolved', "
                "decision_json = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (decision_json, _now(), permission_id),
            )
            if wait_cursor.rowcount != 1:
                raise WaitDecisionConflictError(
                    "permission wait candidate was resolved concurrently"
                )
            if event_payload is not None:
                await self._append_event_uncommitted(
                    conn,
                    str(record["run_id"]),
                    "permission.resolved",
                    payload_json=_encode_payload(event_payload),
                    created_at=_now(),
                )
        return await self.get_permission(permission_id)

    async def consume_run_permission_grant(
        self,
        *,
        run_id: str,
        operation_id: str,
        tool_name: str,
        tool_version: str = "",
        risk: str,
    ) -> bool:
        """Atomically apply one tool-level grant for the rest of this run.

        Every invocation still passes schema, capability, workspace, version,
        and risk checks before reaching this grant.
        """
        async with self.run_write_transaction(run_id) as conn:
            existing = await (
                await conn.execute(
                    "SELECT permission_id FROM local_permission_grant_uses "
                    "WHERE run_id = ? AND operation_id = ?",
                    (run_id, operation_id),
                )
            ).fetchone()
            if existing is not None:
                return True
            grant = await (
                await conn.execute(
                    "SELECT id FROM local_permissions "
                    "WHERE run_id = ? AND tool_name = ? AND tool_version = ? "
                    "AND risk = ? "
                    "AND status = 'approved' AND scope = 'run' "
                    "ORDER BY resolved_at DESC LIMIT 1",
                    (run_id, tool_name, tool_version, risk),
                )
            ).fetchone()
            if grant is None:
                return False
            permission_id = str(grant[0])
            await conn.execute(
                "INSERT INTO local_permission_grant_uses "
                "(permission_id, run_id, operation_id, created_at) VALUES (?, ?, ?, ?)",
                (permission_id, run_id, operation_id, _now()),
            )
            cursor = await conn.execute(
                "UPDATE local_permissions SET grant_use_count = grant_use_count + 1 WHERE id = ?",
                (permission_id,),
            )
            if cursor.rowcount != 1:
                raise PermissionDecisionConflictError("permission grant changed concurrently")
            return True

    async def wait_cycle_resume_payload(
        self,
        *,
        run_id: str,
        wait_cycle_id: str,
    ) -> dict[str, Any] | None:
        """Build LangGraph's interrupt-id keyed resume payload when complete."""
        return await self._wait_cycle_resume_payload_uncommitted(
            self._conn,
            run_id=run_id,
            wait_cycle_id=wait_cycle_id,
        )

    @staticmethod
    async def _wait_cycle_resume_payload_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        wait_cycle_id: str,
    ) -> dict[str, Any] | None:
        cursor = await conn.execute(
            "SELECT * FROM local_wait_candidates "
            "WHERE run_id = ? AND wait_cycle_id = ? "
            "ORDER BY interrupt_id, position",
            (run_id, wait_cycle_id),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        if not rows or any(row.get("status") != "resolved" for row in rows):
            return None
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["interrupt_id"]), []).append(row)
        resume: dict[str, Any] = {}
        for interrupt_id, candidates in grouped.items():
            kinds = {str(candidate.get("kind") or "") for candidate in candidates}
            if kinds == {"tool_review"}:
                resume[interrupt_id] = {
                    "decisions": [
                        json.loads(str(candidate.get("decision_json") or "{}"))
                        for candidate in candidates
                    ]
                }
            elif kinds == {"question"} and len(candidates) == 1:
                resume[interrupt_id] = json.loads(str(candidates[0].get("decision_json") or "{}"))
            elif kinds == {"tool_reconciliation"} and len(candidates) == 1:
                resume[interrupt_id] = json.loads(str(candidates[0].get("decision_json") or "{}"))
            elif kinds == {"plan"} and len(candidates) == 1:
                resume[interrupt_id] = json.loads(str(candidates[0].get("decision_json") or "{}"))
            else:
                raise WaitDecisionConflictError(
                    f"unsupported wait candidate group for interrupt {interrupt_id}"
                )
        return resume

    async def latest_resolved_wait_cycle_payload(self, run_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT wait_cycle_id FROM local_wait_candidates WHERE run_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return await self.wait_cycle_resume_payload(
            run_id=run_id,
            wait_cycle_id=str(row[0]),
        )

    async def list_permissions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_permissions WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [
            {**dict(row), "arguments": json.loads(row["arguments_json"] or "{}")}
            for row in await cursor.fetchall()
        ]

    # --- questions (user.ask interrupt record) ---

    async def create_question(
        self,
        *,
        run_id: str,
        tool_call_id: str | None,
        questions: list[dict[str, Any]],
        wait_cycle_id: str | None = None,
        interrupt_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a `user.ask` interrupt.

        `questions` is a list to allow future multi-question interrupts;
        today user.ask emits one. The returned `id` is the `request_id`
        the client posts back via `POST /v1/questions/{id}` with
        `{answers}`.
        """
        if interrupt_id:
            existing = await (
                await self._conn.execute(
                    "SELECT * FROM local_questions WHERE run_id = ? AND interrupt_id = ?",
                    (run_id, interrupt_id),
                )
            ).fetchone()
            if existing is not None:
                return dict(existing)
        record = {
            "id": _new_id("q"),
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "questions_json": json.dumps(questions, ensure_ascii=False, default=str),
            "status": "pending",
            "answers_json": None,
            "created_at": _now(),
            "answered_at": None,
        }
        record["wait_cycle_id"] = record["wait_cycle_id"] or record["id"]
        record["interrupt_id"] = record["interrupt_id"] or record["id"]
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "INSERT INTO local_questions (id, run_id, tool_call_id, wait_cycle_id, "
                " interrupt_id, questions_json, "
                " status, answers_json, created_at, answered_at) "
                "VALUES (:id, :run_id, :tool_call_id, :wait_cycle_id, :interrupt_id, "
                "        :questions_json, :status, "
                "        :answers_json, :created_at, :answered_at)",
                record,
            )
            await conn.execute(
                "INSERT INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (?, ?, 'question', ?, ?, 0, 'pending', ?, NULL, ?, NULL)",
                (
                    record["id"],
                    run_id,
                    record["wait_cycle_id"],
                    record["interrupt_id"],
                    record["questions_json"],
                    record["created_at"],
                ),
            )
        return record

    async def get_question(self, question_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_questions WHERE id = ?", (question_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def count_questions_for_run(self, run_id: str) -> int:
        row = await (
            await self._conn.execute(
                "SELECT COUNT(*) FROM local_questions WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return int(row[0] if row else 0)

    async def list_answered_question_choices_for_run(
        self,
        *,
        principal_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Return the source run's resolved choices without crossing principals.

        Later answers replace earlier answers for the same question. This keeps
        retry context compact when a failed run asked the same thing repeatedly.
        """
        rows = await (
            await self._conn.execute(
                "SELECT q.answers_json FROM local_questions q "
                "JOIN local_runs r ON r.id = q.run_id "
                "WHERE r.principal_id = ? AND q.run_id = ? "
                "AND q.status = 'answered' AND q.answers_json IS NOT NULL "
                "ORDER BY q.created_at, q.id",
                (principal_id, run_id),
            )
        ).fetchall()
        choices: dict[str, list[str]] = {}
        for row in rows:
            try:
                answers = json.loads(str(row["answers_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(answers, dict):
                continue
            for raw_question, raw_values in answers.items():
                question = str(raw_question).strip()
                if not question:
                    continue
                values = raw_values if isinstance(raw_values, list) else [raw_values]
                normalized = [
                    str(value).strip()
                    for value in values
                    if value is not None and str(value).strip()
                ]
                if normalized:
                    choices[question] = normalized
        return [{"question": question, "answers": answers} for question, answers in choices.items()]

    async def answer_question(
        self,
        question_id: str,
        *,
        answers: dict[str, Any],
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        record = await self.get_question(question_id)
        if record is None:
            return None
        answers_json = json.dumps(
            answers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if record.get("status") != "pending":
            try:
                existing = json.dumps(
                    json.loads(record.get("answers_json") or "{}"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except json.JSONDecodeError:
                existing = str(record.get("answers_json") or "")
            if existing != answers_json:
                raise WaitDecisionConflictError(
                    "question was already answered with different content"
                )
            return record
        now = _now()
        async with self.run_write_transaction(str(record["run_id"])) as conn:
            run = await (
                await conn.execute(
                    "SELECT status FROM local_runs WHERE id = ?",
                    (record["run_id"],),
                )
            ).fetchone()
            if run is None or run["status"] in {
                "completed",
                "failed",
                "canceled",
                "cleanup_required",
            }:
                raise WaitDecisionConflictError("run is not awaiting a decision")
            cursor = await conn.execute(
                "UPDATE local_questions SET status = 'answered', answers_json = ?, "
                "answered_at = ? WHERE id = ? AND status = 'pending'",
                (answers_json, now, question_id),
            )
            wait_cursor = await conn.execute(
                "UPDATE local_wait_candidates SET status = 'resolved', "
                "decision_json = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (answers_json, now, question_id),
            )
            if cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                raise WaitDecisionConflictError("question was answered concurrently")
            if event_payload is not None:
                await self._append_event_uncommitted(
                    conn,
                    str(record["run_id"]),
                    "question.answered",
                    payload_json=_encode_payload(event_payload),
                    created_at=now,
                )
        return await self.get_question(question_id)

    async def list_wait_candidates_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_wait_candidates WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_wait_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_wait_candidates WHERE id = ?", (candidate_id,)
            )
        ).fetchone()
        return dict(row) if row else None

    async def create_tool_reconciliation(
        self,
        *,
        run_id: str,
        operation_id: str,
        wait_cycle_id: str,
        interrupt_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing = await self.get_wait_candidate(operation_id)
        if existing is not None:
            if existing.get("run_id") != run_id or existing.get("kind") != "tool_reconciliation":
                raise WaitDecisionConflictError(
                    "tool reconciliation identity was reused with different content"
                )
            return existing
        record = {
            "id": operation_id,
            "run_id": run_id,
            "kind": "tool_reconciliation",
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "position": 0,
            "status": "pending",
            "payload_json": json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "decision_json": None,
            "created_at": _now(),
            "resolved_at": None,
        }
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "INSERT INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (:id, :run_id, :kind, :wait_cycle_id, :interrupt_id, :position, "
                ":status, :payload_json, :decision_json, :created_at, :resolved_at)",
                record,
            )
        return record

    async def resolve_tool_reconciliation(
        self,
        candidate_id: str,
        *,
        decision: str,
        current_result_json: str | None,
        current_result_hash: str | None,
        prior_result_json: str,
        prior_result_hash: str,
    ) -> dict[str, Any] | None:
        if decision not in {"confirmed_completed", "retry_not_executed", "abort"}:
            raise ValueError(f"invalid tool reconciliation decision: {decision}")
        record = await self.get_wait_candidate(candidate_id)
        if record is None or record.get("kind") != "tool_reconciliation":
            return None
        current_run_id = str(record["run_id"])
        async with self.run_write_transaction(current_run_id) as conn:
            updated, _resolved = await self._resolve_tool_reconciliation_uncommitted(
                conn,
                candidate_id=candidate_id,
                decision=decision,
                current_result_json=current_result_json,
                current_result_hash=current_result_hash,
                prior_result_json=prior_result_json,
                prior_result_hash=prior_result_hash,
            )
            return updated

    async def _resolve_tool_reconciliation_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        candidate_id: str,
        decision: str,
        current_result_json: str | None,
        current_result_hash: str | None,
        prior_result_json: str,
        prior_result_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        record = await (
            await conn.execute(
                "SELECT * FROM local_wait_candidates WHERE id = ?",
                (candidate_id,),
            )
        ).fetchone()
        if record is None or record["kind"] != "tool_reconciliation":
            raise KeyError(candidate_id)
        decision_json = _encode_payload({"decision": decision})
        if record["status"] != "pending":
            if str(record["decision_json"] or "") != decision_json:
                raise WaitDecisionConflictError(
                    "tool reconciliation was already resolved differently"
                )
            return dict(record), False
        payload = _json_payload(record["payload_json"])
        prior_operation_id = str(payload.get("prior_operation_id") or candidate_id)
        current_run_id = str(record["run_id"])
        current_receipt = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                (candidate_id, current_run_id),
            )
        ).fetchone()
        prior_receipt = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                (prior_operation_id,),
            )
        ).fetchone()
        if current_receipt is None or prior_receipt is None:
            raise WaitDecisionConflictError("tool reconciliation receipt is missing")
        prior_run_id = str(prior_receipt["run_id"])
        if prior_run_id != current_run_id:
            ancestor = await (
                await conn.execute(
                    "WITH RECURSIVE lineage(id, owner, depth) AS ("
                    "SELECT parent_run_id, principal_id, 0 FROM local_runs "
                    "WHERE id = ? AND parent_run_id IS NOT NULL UNION ALL "
                    "SELECT parent.parent_run_id, lineage.owner, lineage.depth + 1 "
                    "FROM local_runs AS parent JOIN lineage ON parent.id = lineage.id "
                    "WHERE parent.principal_id = lineage.owner "
                    "AND parent.parent_run_id IS NOT NULL AND lineage.depth < 64"
                    ") SELECT 1 FROM lineage JOIN local_runs AS ancestor "
                    "ON ancestor.id = lineage.id AND ancestor.principal_id = lineage.owner "
                    "WHERE lineage.id = ? LIMIT 1",
                    (current_run_id, prior_run_id),
                )
            ).fetchone()
            if ancestor is None:
                raise WaitDecisionConflictError(
                    "tool reconciliation source is not an owned ancestor"
                )
        now = _now()
        prior_status = "completed" if decision == "confirmed_completed" else "failed"
        prior_cursor = await conn.execute(
            "UPDATE local_tool_receipts SET status = ?, result_json = ?, result_hash = ?, "
            "error_type = ?, completed_at = ?, updated_at = ? "
            "WHERE operation_id = ? AND status = 'outcome_unknown'",
            (
                prior_status,
                prior_result_json,
                prior_result_hash,
                None if prior_status == "completed" else "ReconciledByUser",
                now,
                now,
                prior_operation_id,
            ),
        )
        if prior_cursor.rowcount != 1:
            raise WaitDecisionConflictError(
                "tool reconciliation source is no longer outcome_unknown"
            )
        projected_operation_ids = [prior_operation_id]
        if prior_operation_id == candidate_id and decision == "retry_not_executed":
            await conn.execute(
                "UPDATE local_tool_receipts SET status = 'prepared', result_json = NULL, "
                "result_hash = NULL, error_type = NULL, completed_at = NULL, updated_at = ? "
                "WHERE operation_id = ?",
                (now, candidate_id),
            )
        elif prior_operation_id != candidate_id and decision != "retry_not_executed":
            current_status = "completed" if decision == "confirmed_completed" else "failed"
            current_cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, "
                "result_hash = ?, error_type = ?, completed_at = ?, updated_at = ? "
                "WHERE operation_id = ? AND run_id = ? AND status = 'prepared'",
                (
                    current_status,
                    current_result_json,
                    current_result_hash,
                    None if current_status == "completed" else "ReconciledByUser",
                    now,
                    now,
                    candidate_id,
                    current_run_id,
                ),
            )
            if current_cursor.rowcount != 1:
                raise WaitDecisionConflictError(
                    "current tool reconciliation receipt is no longer prepared"
                )
            projected_operation_ids.append(candidate_id)
        cursor = await conn.execute(
            "UPDATE local_wait_candidates SET status = 'resolved', decision_json = ?, "
            "resolved_at = ? WHERE id = ? AND status = 'pending'",
            (decision_json, now, candidate_id),
        )
        if cursor.rowcount != 1:
            raise WaitDecisionConflictError("tool reconciliation was resolved concurrently")
        for operation_id in projected_operation_ids:
            receipt = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert receipt is not None
            await self._append_subagent_receipt_event_uncommitted(conn, dict(receipt))
        updated = await (
            await conn.execute(
                "SELECT * FROM local_wait_candidates WHERE id = ?",
                (candidate_id,),
            )
        ).fetchone()
        assert updated is not None
        return dict(updated), True

    # --- immutable run inputs and artifacts ---

    async def prepare_run_input_body(self, source_path: Path) -> tuple[int, str, str]:
        """Import one user-selected file without retaining its mutable host path."""
        return await asyncio.to_thread(
            self._promote_blob_body,
            source_path,
            None,
            "inputs",
            MAX_RUN_INPUT_BYTES,
            0o400,
            RunInputSnapshotError,
            RunInputQuotaError,
            "run input",
        )

    async def list_run_inputs(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_run_inputs WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def list_run_inputs_for_runs(self, run_ids: list[str]) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        placeholders = ", ".join("?" for _ in run_ids)
        rows = await (
            await self._conn.execute(
                f"SELECT * FROM local_run_inputs WHERE run_id IN ({placeholders}) "
                "ORDER BY run_id, rowid",
                tuple(run_ids),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    def run_input_body_path(self, run_input: dict[str, Any]) -> Path:
        return self._stored_blob_path(
            run_input,
            root_name="inputs",
            storage_error=RunInputSnapshotError,
            label="run input",
        )

    async def gc_orphan_bodies(
        self,
        *,
        grace_seconds: float = 3600,
        max_scan: int = 10_000,
        max_delete: int = 256,
    ) -> int:
        """Remove old unreferenced bodies left between file promotion and SQL commit."""
        artifact_rows = await (
            await self._conn.execute(
                "SELECT blob_key FROM local_artifacts WHERE storage_kind = 'blob'"
            )
        ).fetchall()
        input_rows = await (
            await self._conn.execute("SELECT blob_key FROM local_run_inputs")
        ).fetchall()
        referenced = {
            "artifacts": {str(row[0]) for row in artifact_rows if row[0]},
            "inputs": {str(row[0]) for row in input_rows if row[0]},
        }
        return await asyncio.to_thread(
            self._gc_orphan_bodies_sync,
            referenced,
            grace_seconds,
            max_scan,
            max_delete,
        )

    def _gc_orphan_bodies_sync(
        self,
        referenced: dict[str, set[str]],
        grace_seconds: float,
        max_scan: int,
        max_delete: int,
    ) -> int:
        cutoff = time.time() - max(0.0, grace_seconds)
        scanned = 0
        deleted = 0
        for root_name in ("artifacts", "inputs"):
            root = self._db_path.parent / root_name
            for candidate in chain(root.glob("sha256/*/*"), root.glob(".tmp/*")):
                if scanned >= max_scan or deleted >= max_delete:
                    return deleted
                scanned += 1
                try:
                    stat = candidate.lstat()
                except FileNotFoundError:
                    continue
                if candidate.is_symlink() or not candidate.is_file() or stat.st_mtime > cutoff:
                    continue
                try:
                    relative = candidate.relative_to(root).as_posix()
                except ValueError:
                    continue
                if relative.startswith(".tmp/") or relative not in referenced[root_name]:
                    candidate.unlink(missing_ok=True)
                    deleted += 1
        return deleted

    # --- artifacts ---

    async def create_artifact(
        self,
        *,
        artifact_id: str | None = None,
        run_id: str,
        kind: str,
        title: str,
        content: str,
        content_type: str = "text/plain",
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        record = {
            "id": artifact_id or _new_id("art"),
            "run_id": run_id,
            "kind": kind,
            "title": title,
            "content": content,
            "content_type": content_type,
            "bytes": len(encoded),
            "storage_kind": "inline_text",
            "blob_key": None,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "metadata_json": _encode_payload(metadata or {}),
            "created_at": _now(),
        }
        if record["bytes"] > MAX_ARTIFACT_BYTES:
            raise ArtifactQuotaError("artifact exceeds the per-item byte limit")
        return await self._create_artifact_record(record, replayable=artifact_id is not None)

    async def create_file_artifact(
        self,
        *,
        source_path: Path,
        run_id: str,
        kind: str,
        title: str,
        content_type: str,
        artifact_id: str | None = None,
        expected_sha256: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        size, digest, blob_key = await asyncio.to_thread(
            self._promote_artifact_body,
            source_path,
            expected_sha256,
        )
        record = {
            "id": artifact_id or _new_id("art"),
            "run_id": run_id,
            "kind": kind,
            "title": title,
            "content": "",
            "content_type": content_type,
            "bytes": size,
            "storage_kind": "blob",
            "blob_key": blob_key,
            "sha256": digest,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "metadata_json": _encode_payload(metadata or {}),
            "created_at": _now(),
        }
        return await self._create_artifact_record(record, replayable=artifact_id is not None)

    def _promote_artifact_body(
        self,
        source_path: Path,
        expected_sha256: str | None,
    ) -> tuple[int, str, str]:
        return self._promote_blob_body(
            source_path,
            expected_sha256,
            "artifacts",
            MAX_BLOB_ARTIFACT_BYTES,
            0o600,
            ArtifactConflictError,
            ArtifactQuotaError,
            "artifact body",
        )

    def _promote_blob_body(
        self,
        source_path: Path,
        expected_sha256: str | None,
        root_name: str,
        max_bytes: int,
        file_mode: int,
        conflict_error: type[RuntimeError],
        quota_error: type[RuntimeError],
        label: str,
    ) -> tuple[int, str, str]:
        source = source_path.resolve(strict=True)
        if source_path.is_symlink() or not source.is_file():
            raise conflict_error(f"{label} is not a regular file")
        root = self._db_path.parent / root_name
        temporary_root = root / ".tmp"
        temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = temporary_root / uuid.uuid4().hex
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise quota_error(f"{label} exceeds the per-item byte limit")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            actual_sha256 = digest.hexdigest()
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise conflict_error(f"{label} digest changed before promotion")
            blob_key = f"sha256/{actual_sha256[:2]}/{actual_sha256}"
            destination = root.joinpath(*PurePosixPath(blob_key).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    raise conflict_error(f"{label} store entry is invalid")
                existing_size, existing_digest = _file_identity(destination)
                if existing_size != size or existing_digest != actual_sha256:
                    raise conflict_error(f"{label} store entry is corrupt")
                temporary.unlink()
            else:
                os.replace(temporary, destination)
                destination.chmod(file_mode)
            return size, actual_sha256, blob_key
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def _create_artifact_record(
        self,
        record: dict[str, Any],
        *,
        replayable: bool,
    ) -> dict[str, Any]:
        immutable_fields = (
            "run_id",
            "kind",
            "title",
            "content",
            "content_type",
            "bytes",
            "storage_kind",
            "blob_key",
            "sha256",
            "tool_call_id",
            "tool_name",
            "metadata_json",
        )

        def reconcile_replay(row: aiosqlite.Row) -> dict[str, Any]:
            persisted = dict(row)
            if any(persisted[field] != record[field] for field in immutable_fields):
                raise ArtifactConflictError(
                    f"artifact {record['id']} was replayed with different content"
                )
            return persisted

        run_id = str(record["run_id"])
        async with self.run_write_transaction(run_id) as conn:
            if replayable:
                existing = await (
                    await conn.execute(
                        "SELECT * FROM local_artifacts WHERE id = ?",
                        (record["id"],),
                    )
                ).fetchone()
                if existing is not None:
                    return reconcile_replay(existing)
            owner = await (
                await conn.execute("SELECT principal_id FROM local_runs WHERE id = ?", (run_id,))
            ).fetchone()
            if owner is None:
                raise KeyError(f"unknown run: {run_id}")
            run_bytes = int(
                (
                    await (
                        await conn.execute(
                            "SELECT COALESCE(SUM(bytes), 0) FROM local_artifacts WHERE run_id = ?",
                            (run_id,),
                        )
                    ).fetchone()
                )[0]
            )
            principal_bytes = int(
                (
                    await (
                        await conn.execute(
                            "SELECT COALESCE(SUM(local_artifacts.bytes), 0) "
                            "FROM local_artifacts JOIN local_runs "
                            "ON local_runs.id = local_artifacts.run_id "
                            "WHERE local_runs.principal_id = ?",
                            (owner[0],),
                        )
                    ).fetchone()
                )[0]
            )
            total_bytes = int(
                (
                    await (
                        await conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM local_artifacts")
                    ).fetchone()
                )[0]
            )
            if run_bytes + record["bytes"] > MAX_RUN_ARTIFACT_BYTES:
                raise ArtifactQuotaError("run artifact byte limit exceeded")
            if principal_bytes + record["bytes"] > MAX_PRINCIPAL_ARTIFACT_BYTES:
                raise ArtifactQuotaError("principal artifact byte limit exceeded")
            if total_bytes + record["bytes"] > MAX_TOTAL_ARTIFACT_BYTES:
                raise ArtifactQuotaError("local artifact storage byte limit exceeded")
            cursor = await conn.execute(
                f"INSERT {'OR IGNORE ' if replayable else ''}INTO local_artifacts "
                "(id, run_id, kind, title, content, "
                " content_type, bytes, storage_kind, blob_key, sha256, tool_call_id, "
                " tool_name, metadata_json, created_at) "
                "VALUES (:id, :run_id, :kind, :title, :content, :content_type, :bytes, "
                "        :storage_kind, :blob_key, :sha256, :tool_call_id, :tool_name, "
                "        :metadata_json, :created_at)",
                record,
            )
            if cursor.rowcount == 0:
                existing = await (
                    await conn.execute(
                        "SELECT * FROM local_artifacts WHERE id = ?",
                        (record["id"],),
                    )
                ).fetchone()
                if existing is None:
                    raise ArtifactConflictError("artifact identity could not be reconciled")
                return reconcile_replay(existing)
        return record

    def artifact_body_path(self, artifact: dict[str, Any]) -> Path:
        if artifact.get("storage_kind") != "blob" or not isinstance(artifact.get("blob_key"), str):
            raise ArtifactConflictError("artifact does not have a blob body")
        return self._stored_blob_path(
            artifact,
            root_name="artifacts",
            storage_error=ArtifactConflictError,
            label="artifact blob body",
        )

    def _stored_blob_path(
        self,
        record: dict[str, Any],
        *,
        root_name: str,
        storage_error: type[RuntimeError],
        label: str,
    ) -> Path:
        blob_key = record.get("blob_key")
        if not isinstance(blob_key, str):
            raise storage_error(f"{label} key is missing")
        relative = PurePosixPath(blob_key)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise storage_error(f"{label} key is invalid")
        root = (self._db_path.parent / root_name).resolve(strict=True)
        candidate = root.joinpath(*relative.parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise storage_error(f"{label} is missing")
        try:
            candidate.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise storage_error(f"{label} escaped storage") from exc
        if candidate.stat().st_size != int(record["bytes"]):
            raise storage_error(f"{label} size changed")
        return candidate

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_artifacts WHERE id = ?", (artifact_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_artifacts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_artifacts WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [
            {**dict(row), "metadata": json.loads(row["metadata_json"] or "{}")}
            for row in await cursor.fetchall()
        ]
