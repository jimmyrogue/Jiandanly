"""SQLite schema for the Runtime-owned local store.

Keep declarative DDL here. Ordered compatibility migrations live in
`migrations.py`; transaction behavior stays in `database.py`.
"""

from __future__ import annotations

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
