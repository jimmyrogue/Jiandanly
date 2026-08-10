"""A2A Gateway SQLite schema and compatibility migrations."""

from __future__ import annotations

import aiosqlite

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS a2a_peers (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    tenant TEXT NOT NULL UNIQUE,
    oidc_issuer TEXT,
    oidc_subject TEXT,
    token_digest TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    runtime_model TEXT NOT NULL,
    runtime_workspace_path TEXT,
    permission_mode TEXT NOT NULL CHECK (permission_mode IN ('ask', 'auto', 'full_access')),
    push_origins_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS a2a_tasks (
    id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    context_id TEXT NOT NULL,
    runtime_run_id TEXT UNIQUE,
    runtime_thread_id TEXT NOT NULL,
    create_command_id TEXT NOT NULL UNIQUE,
    create_client_message_id TEXT NOT NULL UNIQUE,
    output_mode TEXT NOT NULL DEFAULT 'text/plain',
    admission_status TEXT NOT NULL CHECK (admission_status IN ('pending', 'accepted', 'rejected')),
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_peer_context
    ON a2a_tasks(peer_id, tenant, context_id, created_at, id);

CREATE TABLE IF NOT EXISTS a2a_messages (
    peer_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    task_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    message_json TEXT NOT NULL,
    runtime_command_id TEXT NOT NULL UNIQUE,
    runtime_instruction_id TEXT,
    delivery_status TEXT NOT NULL CHECK (delivery_status IN ('pending', 'accepted', 'rejected')),
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (peer_id, message_id),
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES a2a_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_a2a_messages_task
    ON a2a_messages(peer_id, task_id, created_at, message_id);

CREATE TABLE IF NOT EXISTS a2a_artifacts (
    id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    task_id TEXT NOT NULL,
    runtime_artifact_id TEXT NOT NULL,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    storage_kind TEXT NOT NULL CHECK (storage_kind IN ('inline_text', 'blob')),
    inline_content TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (peer_id, task_id, runtime_artifact_id),
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES a2a_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_a2a_artifacts_task
    ON a2a_artifacts(peer_id, task_id, created_at, id);

CREATE TABLE IF NOT EXISTS a2a_push_configs (
    id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    task_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    url TEXT NOT NULL,
    token_ciphertext TEXT,
    auth_scheme TEXT,
    credentials_ciphertext TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    PRIMARY KEY (peer_id, task_id, id),
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES a2a_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_a2a_push_configs_task
    ON a2a_push_configs(peer_id, task_id, created_at, id);

CREATE TABLE IF NOT EXISTS a2a_push_cursors (
    task_id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    runtime_after INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES a2a_tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS a2a_push_outbox (
    id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'leased', 'delivered', 'dead', 'canceled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (peer_id, task_id, config_id, event_key),
    FOREIGN KEY (peer_id, task_id, config_id)
        REFERENCES a2a_push_configs(peer_id, task_id, id) ON DELETE CASCADE,
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES a2a_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_a2a_push_outbox_ready
    ON a2a_push_outbox(status, available_at, lease_until, created_at);

CREATE TABLE IF NOT EXISTS a2a_request_rate_windows (
    peer_id TEXT NOT NULL,
    window_epoch_minute INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    PRIMARY KEY (peer_id, window_epoch_minute),
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS a2a_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    peer_id TEXT,
    tenant TEXT,
    trace_id TEXT NOT NULL,
    http_method TEXT NOT NULL,
    path TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    content_length INTEGER,
    FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_a2a_audit_events_peer_time
    ON a2a_audit_events(peer_id, occurred_at, id);
"""


async def _migrate_task_scoped_push_config_ids(conn: aiosqlite.Connection) -> None:
    columns = await (await conn.execute("PRAGMA table_info(a2a_push_configs)")).fetchall()
    primary_key = [
        str(row["name"])
        for row in sorted(columns, key=lambda row: int(row["pk"]))
        if int(row["pk"]) > 0
    ]
    if primary_key == ["peer_id", "task_id", "id"]:
        return
    if primary_key != ["id"]:
        raise RuntimeError(f"unsupported a2a_push_configs primary key: {primary_key}")

    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "CREATE TABLE a2a_push_configs_v2 ("
            "id TEXT NOT NULL, peer_id TEXT NOT NULL, tenant TEXT NOT NULL, "
            "task_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL, url TEXT NOT NULL, "
            "token_ciphertext TEXT, auth_scheme TEXT, credentials_ciphertext TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT, "
            "PRIMARY KEY (peer_id, task_id, id), "
            "FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE, "
            "FOREIGN KEY (task_id) REFERENCES a2a_tasks(id) ON DELETE CASCADE)"
        )
        await conn.execute("INSERT INTO a2a_push_configs_v2 SELECT * FROM a2a_push_configs")
        await conn.execute(
            "CREATE TABLE a2a_push_outbox_v2 ("
            "id TEXT PRIMARY KEY, config_id TEXT NOT NULL, peer_id TEXT NOT NULL, "
            "task_id TEXT NOT NULL, event_key TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN "
            "('pending', 'leased', 'delivered', 'dead', 'canceled')), "
            "attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, "
            "lease_until TEXT, last_error TEXT, created_at TEXT NOT NULL, delivered_at TEXT, "
            "UNIQUE (peer_id, task_id, config_id, event_key), "
            "FOREIGN KEY (peer_id, task_id, config_id) REFERENCES "
            "a2a_push_configs_v2(peer_id, task_id, id) ON DELETE CASCADE, "
            "FOREIGN KEY (peer_id) REFERENCES a2a_peers(id) ON DELETE CASCADE, "
            "FOREIGN KEY (task_id) REFERENCES a2a_tasks(id) ON DELETE CASCADE)"
        )
        await conn.execute("INSERT INTO a2a_push_outbox_v2 SELECT * FROM a2a_push_outbox")
        await conn.execute("DROP TABLE a2a_push_outbox")
        await conn.execute("DROP TABLE a2a_push_configs")
        await conn.execute("ALTER TABLE a2a_push_configs_v2 RENAME TO a2a_push_configs")
        await conn.execute("ALTER TABLE a2a_push_outbox_v2 RENAME TO a2a_push_outbox")
        await conn.execute(
            "CREATE INDEX idx_a2a_push_configs_task ON "
            "a2a_push_configs(peer_id, task_id, created_at, id)"
        )
        await conn.execute(
            "CREATE INDEX idx_a2a_push_outbox_ready ON "
            "a2a_push_outbox(status, available_at, lease_until, created_at)"
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")

    violations = await (await conn.execute("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError("A2A push configuration migration violated foreign keys")


async def _migrate_oidc_peer_identity(conn: aiosqlite.Connection) -> None:
    columns = {
        str(row["name"])
        for row in await (await conn.execute("PRAGMA table_info(a2a_peers)")).fetchall()
    }
    if "oidc_issuer" not in columns:
        await conn.execute("ALTER TABLE a2a_peers ADD COLUMN oidc_issuer TEXT")
    if "oidc_subject" not in columns:
        await conn.execute("ALTER TABLE a2a_peers ADD COLUMN oidc_subject TEXT")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_peers_oidc_identity "
        "ON a2a_peers(oidc_issuer, oidc_subject) "
        "WHERE oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL"
    )


async def initialize_a2a_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA)
    await _migrate_oidc_peer_identity(conn)
    await _migrate_task_scoped_push_config_ids(conn)
    columns = {
        str(row["name"])
        for row in await (await conn.execute("PRAGMA table_info(a2a_tasks)")).fetchall()
    }
    if "output_mode" not in columns:
        await conn.execute(
            "ALTER TABLE a2a_tasks ADD COLUMN output_mode TEXT NOT NULL DEFAULT 'text/plain'"
        )
