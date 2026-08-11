"""Ordered compatibility migrations for existing Runtime SQLite databases.

These functions intentionally remain explicit transaction-safe SQL. Do not
introduce a second migration framework or let a migration commit the caller's
outer transaction.
"""

from __future__ import annotations

import json

import aiosqlite

from ..auth import LOCAL_OWNER_PRINCIPAL_ID
from ..model_services.credentials import delete_legacy_model_api_key
from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .events import TRANSIENT_RUN_EVENT_TYPES


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


async def _delete_legacy_model_provider_credentials(
    conn: aiosqlite.Connection,
) -> None:
    table = await (
        await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'local_model_providers'"
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


async def _ensure_columns(conn: aiosqlite.Connection) -> None:
    """Additive migrations for DBs created before a column existed.
    `CREATE TABLE IF NOT EXISTS` never alters an existing table, so a new
    column has to be added explicitly. SQLite ADD COLUMN is cheap + safe."""
    await _ensure_principal_scoped_workspaces(conn)
    await conn.execute("DROP TABLE IF EXISTS local_model_providers")
    cursor = await conn.execute("PRAGMA table_info(local_runs)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "mode" not in columns:
        await conn.execute("ALTER TABLE local_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'fast'")
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
            "ALTER TABLE local_runs ADD COLUMN agent_definition_version TEXT NOT NULL DEFAULT '1'"
        )
    if "collaboration_depth" not in columns:
        await conn.execute(
            "ALTER TABLE local_runs ADD COLUMN collaboration_depth INTEGER NOT NULL "
            "DEFAULT 0 CHECK (collaboration_depth >= 0)"
        )
    if "collaboration_policy_json" not in columns:
        await conn.execute(
            "ALTER TABLE local_runs ADD COLUMN collaboration_policy_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "spawn_operation_id" not in columns:
        await conn.execute("ALTER TABLE local_runs ADD COLUMN spawn_operation_id TEXT")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_runs_parent_kind "
        "ON local_runs(parent_run_id, run_kind, created_at, id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_runs_root ON local_runs(root_run_id, created_at, id)"
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
        await conn.execute("ALTER TABLE local_thread_items ADD COLUMN superseded_by_run_id TEXT")
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
        "CREATE INDEX idx_local_thread_items_order ON local_thread_items(thread_id, position, id)"
    )
    cursor = await conn.execute("PRAGMA table_info(local_scheduled_runs)")
    schedule_columns = {row[1] for row in await cursor.fetchall()}
    if "principal_id" not in schedule_columns:
        await conn.execute(
            "ALTER TABLE local_scheduled_runs ADD COLUMN principal_id "
            "TEXT NOT NULL DEFAULT 'local:owner'"
        )
    await _ensure_principal_scoped_commands(conn)
    await _ensure_generic_commands(conn)
    cursor = await conn.execute("PRAGMA table_info(plugin_installations)")
    plugin_installation_columns = {row[1] for row in await cursor.fetchall()}
    if "retired_at" not in plugin_installation_columns:
        await conn.execute("ALTER TABLE plugin_installations ADD COLUMN retired_at TEXT")
    if "model_binding_json" not in plugin_installation_columns:
        await conn.execute("ALTER TABLE plugin_installations ADD COLUMN model_binding_json TEXT")
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
    await _ensure_run_job_principals(conn)
    cursor = await conn.execute("PRAGMA table_info(local_run_jobs)")
    job_columns = {row[1] for row in await cursor.fetchall()}
    if "quarantined_at" not in job_columns:
        await conn.execute("ALTER TABLE local_run_jobs ADD COLUMN quarantined_at TEXT")
    if "quarantine_reason" not in job_columns:
        await conn.execute("ALTER TABLE local_run_jobs ADD COLUMN quarantine_reason TEXT")
    await _ensure_permission_identity_columns(conn)
    await _ensure_wait_identity_columns(conn)
    await _ensure_tool_receipt_namespace(conn)
    await _ensure_tool_receipt_version_column(conn)
    await _ensure_tool_receipt_review_columns(conn)
    await _ensure_tool_receipt_parent_column(conn)
    await _ensure_model_call_purpose_column(conn)
    await _ensure_model_call_parent_operation_column(conn)
    await _ensure_model_call_retry_columns(conn)
    await _ensure_model_call_usage_column(conn)
    await _ensure_model_call_phase_columns(conn)
    await _ensure_wait_candidates(conn)
    await _ensure_artifact_storage_columns(conn)
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
    await _ensure_event_sequence_index(conn)


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
            await conn.execute(f"ALTER TABLE local_permissions ADD COLUMN {column} {definition}")
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
    await _backfill_plan_approval_wait_identity(conn)


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
                "UPDATE local_plan_approvals SET wait_cycle_id = ?, interrupt_id = ? WHERE id = ?",
                (*identity, plan[0]),
            )


async def _ensure_tool_receipt_version_column(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(local_tool_receipts)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "tool_version" not in columns:
        await conn.execute(
            "ALTER TABLE local_tool_receipts ADD COLUMN tool_version TEXT NOT NULL DEFAULT ''"
        )


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


async def _ensure_tool_receipt_parent_column(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(local_tool_receipts)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "parent_operation_id" not in columns:
        await conn.execute("ALTER TABLE local_tool_receipts ADD COLUMN parent_operation_id TEXT")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_tool_receipts_parent "
        "ON local_tool_receipts(parent_operation_id, created_at)"
    )


async def _ensure_model_call_purpose_column(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(local_model_calls)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "purpose" not in columns:
        await conn.execute(
            "ALTER TABLE local_model_calls ADD COLUMN purpose TEXT NOT NULL DEFAULT 'agent'"
        )


async def _ensure_model_call_parent_operation_column(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(local_model_calls)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "parent_tool_operation_id" not in columns:
        await conn.execute("ALTER TABLE local_model_calls ADD COLUMN parent_tool_operation_id TEXT")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_model_calls_parent_tool "
        "ON local_model_calls(parent_tool_operation_id, call_index)"
    )


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


async def _ensure_model_call_usage_column(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(local_model_calls)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "usage_json" not in columns:
        await conn.execute(
            "ALTER TABLE local_model_calls ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'"
        )


async def _ensure_model_call_phase_columns(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(local_model_calls)")
    columns = {row[1] for row in await cursor.fetchall()}
    additions = (
        ("phase", "TEXT NOT NULL DEFAULT 'waiting_provider'"),
        ("phase_started_at", "TEXT"),
        ("request_started_at", "TEXT"),
        ("response_headers_at", "TEXT"),
        ("first_raw_chunk_at", "TEXT"),
        ("reasoning_started_at", "TEXT"),
        ("first_visible_output_at", "TEXT"),
        ("reasoning_tokens", "INTEGER"),
    )
    for name, definition in additions:
        if name not in columns:
            await conn.execute(f"ALTER TABLE local_model_calls ADD COLUMN {name} {definition}")
    await conn.execute(
        "UPDATE local_model_calls SET phase_started_at = COALESCE(phase_started_at, created_at), "
        "request_started_at = COALESCE(request_started_at, created_at), "
        "first_visible_output_at = COALESCE(first_visible_output_at, first_output_at)"
    )


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
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_events_run_seq ON local_events(run_id, seq)"
    )
