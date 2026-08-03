from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

_TOKEN_PREFIX = "sj_a2a"
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUNTIME_MODEL_RE = re.compile(r"^local:[^:\s]+:\S+$")
_ALLOWED_SCOPES = frozenset({"tasks.create", "tasks.read", "tasks.cancel", "push.manage"})
_PERMISSION_MODES = frozenset({"ask", "auto", "full_access"})

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _token_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _new_token(peer_id: str) -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    return f"{_TOKEN_PREFIX}.{peer_id}.{secret}", _token_digest(secret)


def _required_text(value: str, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or "\x00" in normalized:
        raise ValueError(f"{field} must contain 1 to {max_length} valid characters")
    return normalized


def _normalize_push_origin(value: str) -> str:
    raw = _required_text(value, field="push origin", max_length=2048)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("push origin is invalid") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("push origin must use HTTPS")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("push origin must be an HTTPS origin without credentials or a path")

    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ValueError("push origin host is invalid") from exc
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("push origin host must be public")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("push origin host must be public") from None
    else:
        if not address.is_global:
            raise ValueError("push origin host must be public")
        if address.version == 6:
            host = f"[{host}]"
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("push origin port is invalid")
    return f"https://{host}{f':{port}' if port not in {None, 443} else ''}"


def _normalize_expiry(value: str | None) -> str | None:
    if value is None:
        return None
    raw = _required_text(value, field="expires_at", max_length=64)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expires_at must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _normalize_oidc_identity(
    issuer: str | None, subject: str | None
) -> tuple[str | None, str | None]:
    if issuer is None and subject is None:
        return None, None
    if issuer is None or subject is None:
        raise ValueError("OIDC issuer and subject must be configured together")
    normalized_issuer = _required_text(issuer, field="OIDC issuer", max_length=2048)
    try:
        parsed = urlsplit(normalized_issuer)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("OIDC issuer is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OIDC issuer must be an HTTPS URL without credentials, query, or fragment")
    normalized_subject = _required_text(subject, field="OIDC subject", max_length=512)
    return normalized_issuer, normalized_subject


def _decode_peer(row: aiosqlite.Row) -> dict[str, Any]:
    record = dict(row)
    record["scopes"] = json.loads(str(record.pop("scopes_json")))
    record["push_origins"] = json.loads(str(record.pop("push_origins_json")))
    record.pop("token_digest", None)
    return record


def _decode_message(row: aiosqlite.Row) -> dict[str, Any]:
    record = dict(row)
    record["message"] = json.loads(str(record.pop("message_json")))
    return record


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


class A2AMessageConflictError(RuntimeError):
    """A peer reused a message id with different immutable content."""


class A2APushConfigConflictError(RuntimeError):
    """A peer reused a push config id with different immutable content."""


class A2AGatewayStore:
    def __init__(
        self,
        db_path: Path,
        connection: aiosqlite.Connection,
        transaction_connection: aiosqlite.Connection,
    ) -> None:
        self.db_path = db_path
        self._conn = connection
        self._transaction_conn = transaction_connection
        self._transaction_lock = asyncio.Lock()

    @classmethod
    async def open(cls, db_path: Path) -> A2AGatewayStore:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA)
        await _migrate_oidc_peer_identity(conn)
        await _migrate_task_scoped_push_config_ids(conn)
        transaction_conn = await aiosqlite.connect(str(path), isolation_level=None)
        transaction_conn.row_factory = aiosqlite.Row
        await transaction_conn.execute("PRAGMA busy_timeout=5000")
        await transaction_conn.execute("PRAGMA foreign_keys=ON")
        columns = {
            str(row["name"])
            for row in await (await conn.execute("PRAGMA table_info(a2a_tasks)")).fetchall()
        }
        if "output_mode" not in columns:
            await conn.execute(
                "ALTER TABLE a2a_tasks ADD COLUMN output_mode TEXT NOT NULL DEFAULT 'text/plain'"
            )
        return cls(path, conn, transaction_conn)

    async def close(self) -> None:
        await self._transaction_conn.close()
        await self._conn.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._transaction_lock:
            await self._transaction_conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._transaction_conn
            except BaseException:
                await self._transaction_conn.rollback()
                raise
            else:
                await self._transaction_conn.commit()

    async def create_peer(
        self,
        *,
        name: str,
        tenant: str,
        scopes: list[str],
        runtime_model: str,
        runtime_workspace_path: str | None,
        permission_mode: str,
        push_origins: list[str],
        expires_at: str | None,
        oidc_issuer: str | None = None,
        oidc_subject: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        normalized_name = _required_text(name, field="name", max_length=128)
        normalized_tenant = _required_text(tenant, field="tenant", max_length=64)
        if _TENANT_RE.fullmatch(normalized_tenant) is None:
            raise ValueError("tenant must use letters, numbers, dots, underscores, or hyphens")
        if not isinstance(scopes, list) or not scopes:
            raise ValueError("at least one A2A peer scope is required")
        if any(not isinstance(scope, str) or scope not in _ALLOWED_SCOPES for scope in scopes):
            raise ValueError("scope is not supported")
        normalized_scopes = sorted(set(scopes))
        normalized_model = _required_text(runtime_model, field="runtime_model", max_length=512)
        if _RUNTIME_MODEL_RE.fullmatch(normalized_model) is None:
            raise ValueError("runtime_model must be local:<connection>:<model>")
        normalized_workspace: str | None = None
        if runtime_workspace_path is not None:
            normalized_workspace = _required_text(
                runtime_workspace_path,
                field="runtime workspace path",
                max_length=4096,
            )
            if not Path(normalized_workspace).is_absolute():
                raise ValueError("runtime workspace path must be absolute")
        if permission_mode not in _PERMISSION_MODES:
            raise ValueError("permission_mode is not supported")
        if not isinstance(push_origins, list) or len(push_origins) > 16:
            raise ValueError("push origins must be a list with at most 16 entries")
        normalized_origins = sorted({_normalize_push_origin(origin) for origin in push_origins})
        normalized_expiry = _normalize_expiry(expires_at)
        normalized_oidc_issuer, normalized_oidc_subject = _normalize_oidc_identity(
            oidc_issuer, oidc_subject
        )

        peer_id = f"peer_{uuid.uuid4().hex}"
        token, digest = _new_token(peer_id)
        now = _now()
        try:
            await self._conn.execute(
                "INSERT INTO a2a_peers "
                "(id, principal_id, name, tenant, oidc_issuer, oidc_subject, token_digest, "
                "scopes_json, runtime_model, runtime_workspace_path, permission_mode, "
                "push_origins_json, created_at, updated_at, expires_at, revoked_at, "
                "last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    peer_id,
                    f"a2a:{peer_id}",
                    normalized_name,
                    normalized_tenant,
                    normalized_oidc_issuer,
                    normalized_oidc_subject,
                    digest,
                    json.dumps(normalized_scopes, separators=(",", ":")),
                    normalized_model,
                    normalized_workspace,
                    permission_mode,
                    json.dumps(normalized_origins, separators=(",", ":")),
                    now,
                    now,
                    normalized_expiry,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "a2a_peers.tenant" in str(exc):
                raise ValueError("tenant already exists") from exc
            if "a2a_peers.oidc_issuer, a2a_peers.oidc_subject" in str(exc):
                raise ValueError("OIDC identity already belongs to another peer") from exc
            raise
        peer = await self.get_peer(peer_id)
        assert peer is not None
        return peer, token

    async def get_peer(self, peer_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute("SELECT * FROM a2a_peers WHERE id = ?", (peer_id,))
        ).fetchone()
        return _decode_peer(row) if row is not None else None

    async def list_peers(self) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute("SELECT * FROM a2a_peers ORDER BY created_at, id")
        ).fetchall()
        return [_decode_peer(row) for row in rows]

    async def consume_request_rate(
        self,
        *,
        peer_id: str,
        limit_per_minute: int,
        epoch_seconds: int,
    ) -> tuple[bool, int]:
        window = epoch_seconds // 60
        retry_after = 60 - (epoch_seconds % 60)
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO a2a_request_rate_windows "
                "(peer_id, window_epoch_minute, request_count) VALUES (?, ?, 1) "
                "ON CONFLICT(peer_id, window_epoch_minute) DO UPDATE SET "
                "request_count = request_count + 1 WHERE request_count < ?",
                (peer_id, window, limit_per_minute),
            )
            await conn.execute(
                "DELETE FROM a2a_request_rate_windows WHERE window_epoch_minute < ?",
                (window - 120,),
            )
            return cursor.rowcount == 1, retry_after

    async def append_audit_event(
        self,
        *,
        peer_id: str | None,
        tenant: str | None,
        trace_id: str,
        http_method: str,
        path: str,
        http_status: int,
        duration_ms: int,
        content_length: int | None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO a2a_audit_events "
            "(occurred_at, peer_id, tenant, trace_id, http_method, path, http_status, "
            "duration_ms, content_length) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                peer_id,
                tenant,
                trace_id,
                http_method,
                path,
                http_status,
                duration_ms,
                content_length,
            ),
        )

    async def list_audit_events(self, *, peer_id: str | None = None) -> list[dict[str, Any]]:
        if peer_id is None:
            rows = await (
                await self._conn.execute("SELECT * FROM a2a_audit_events ORDER BY id")
            ).fetchall()
        else:
            rows = await (
                await self._conn.execute(
                    "SELECT * FROM a2a_audit_events WHERE peer_id = ? ORDER BY id",
                    (peer_id,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def authenticate_peer(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str) or len(token) > 256:
            return None
        parts = token.split(".", 2)
        if len(parts) != 3 or parts[0] != _TOKEN_PREFIX or not parts[1] or not parts[2]:
            return None
        peer_id, secret = parts[1], parts[2]
        row = await (
            await self._conn.execute("SELECT * FROM a2a_peers WHERE id = ?", (peer_id,))
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None:
            try:
                expiry = datetime.fromisoformat(str(expires_at))
            except ValueError:
                return None
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                return None
        if not secrets.compare_digest(str(row["token_digest"]), _token_digest(secret)):
            return None
        now = _now()
        await self._conn.execute(
            "UPDATE a2a_peers SET last_used_at = ?, updated_at = ? WHERE id = ?",
            (now, now, peer_id),
        )
        peer = _decode_peer(row)
        peer["last_used_at"] = now
        peer["updated_at"] = now
        return peer

    async def authenticate_oidc_peer(self, *, issuer: str, subject: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_peers WHERE oidc_issuer = ? AND oidc_subject = ?",
                (issuer, subject),
            )
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None:
            try:
                expiry = datetime.fromisoformat(str(expires_at))
            except ValueError:
                return None
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                return None
        now = _now()
        await self._conn.execute(
            "UPDATE a2a_peers SET last_used_at = ?, updated_at = ? WHERE id = ?",
            (now, now, row["id"]),
        )
        peer = _decode_peer(row)
        peer["last_used_at"] = now
        peer["updated_at"] = now
        return peer

    async def rotate_peer_token(self, peer_id: str) -> str:
        token, digest = _new_token(peer_id)
        cursor = await self._conn.execute(
            "UPDATE a2a_peers SET token_digest = ?, updated_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (digest, _now(), peer_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(peer_id)
        return token

    async def revoke_peer(self, peer_id: str) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_peers SET revoked_at = COALESCE(revoked_at, ?), updated_at = ? "
            "WHERE id = ?",
            (now, now, peer_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(peer_id)
        peer = await self.get_peer(peer_id)
        assert peer is not None
        return peer

    async def prepare_message(
        self,
        *,
        peer_id: str,
        tenant: str,
        message_id: str,
        task_id: str | None,
        context_id: str | None,
        reference_task_ids: list[str],
        request_fingerprint: str,
        message: dict[str, Any],
        new_task_id: str,
        new_context_id: str,
        runtime_thread_id: str,
        runtime_command_id: str,
        runtime_client_message_id: str,
        output_mode: str = "text/plain",
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        async with self._transaction() as conn:
            existing = await (
                await conn.execute(
                    "SELECT * FROM a2a_messages WHERE peer_id = ? AND message_id = ?",
                    (peer_id, message_id),
                )
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"]) != request_fingerprint:
                    raise A2AMessageConflictError(
                        f"message {message_id} was already accepted with different content"
                    )
                task = await (
                    await conn.execute(
                        "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                        (existing["task_id"], peer_id, tenant),
                    )
                ).fetchone()
                if task is None:
                    raise RuntimeError(f"message {message_id} references a missing task")
                return dict(task), _decode_message(existing), False

            selected_task: aiosqlite.Row | None = None
            selected_context_id = context_id
            if task_id:
                selected_task = await (
                    await conn.execute(
                        "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                        (task_id, peer_id, tenant),
                    )
                ).fetchone()
                if selected_task is None:
                    raise KeyError(task_id)
                inferred_context = str(selected_task["context_id"])
                if selected_context_id and selected_context_id != inferred_context:
                    raise ValueError("message context_id does not match its task")
                selected_context_id = inferred_context
            elif selected_context_id:
                known_context = await (
                    await conn.execute(
                        "SELECT 1 FROM a2a_tasks "
                        "WHERE peer_id = ? AND tenant = ? AND context_id = ? LIMIT 1",
                        (peer_id, tenant, selected_context_id),
                    )
                ).fetchone()
                if known_context is None:
                    raise KeyError(selected_context_id)
            else:
                selected_context_id = new_context_id

            if reference_task_ids:
                placeholders = ",".join("?" for _ in reference_task_ids)
                rows = await (
                    await conn.execute(
                        f"SELECT id FROM a2a_tasks WHERE peer_id = ? AND tenant = ? "
                        f"AND id IN ({placeholders})",
                        (peer_id, tenant, *reference_task_ids),
                    )
                ).fetchall()
                if {str(row["id"]) for row in rows} != set(reference_task_ids):
                    raise KeyError("referenced task not found")

            now = _now()
            created_task = selected_task is None
            if created_task:
                await conn.execute(
                    "INSERT INTO a2a_tasks "
                    "(id, peer_id, tenant, context_id, runtime_run_id, runtime_thread_id, "
                    "create_command_id, create_client_message_id, output_mode, admission_status, "
                    "rejection_reason, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, 'pending', NULL, ?, ?)",
                    (
                        new_task_id,
                        peer_id,
                        tenant,
                        selected_context_id,
                        runtime_thread_id,
                        runtime_command_id,
                        runtime_client_message_id,
                        output_mode,
                        now,
                        now,
                    ),
                )
                task_id = new_task_id
            else:
                task_id = str(selected_task["id"])

            normalized_message = dict(message)
            normalized_message["taskId"] = task_id
            normalized_message["contextId"] = selected_context_id
            await conn.execute(
                "INSERT INTO a2a_messages "
                "(peer_id, message_id, tenant, task_id, context_id, request_fingerprint, "
                "message_json, runtime_command_id, runtime_instruction_id, delivery_status, "
                "rejection_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', NULL, ?, ?)",
                (
                    peer_id,
                    message_id,
                    tenant,
                    task_id,
                    selected_context_id,
                    request_fingerprint,
                    json.dumps(normalized_message, ensure_ascii=False, separators=(",", ":")),
                    runtime_command_id,
                    now,
                    now,
                ),
            )
            task = await (
                await conn.execute(
                    "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                    (task_id, peer_id, tenant),
                )
            ).fetchone()
            stored_message = await (
                await conn.execute(
                    "SELECT * FROM a2a_messages WHERE peer_id = ? AND message_id = ?",
                    (peer_id, message_id),
                )
            ).fetchone()
            assert task is not None and stored_message is not None
            return dict(task), _decode_message(stored_message), created_task

    async def get_task(self, *, peer_id: str, tenant: str, task_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                (task_id, peer_id, tenant),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_tasks(self, *, peer_id: str, tenant: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE peer_id = ? AND tenant = ? "
                "ORDER BY created_at DESC, id DESC",
                (peer_id, tenant),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_message(self, *, peer_id: str, message_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_messages WHERE peer_id = ? AND message_id = ?",
                (peer_id, message_id),
            )
        ).fetchone()
        return _decode_message(row) if row is not None else None

    async def list_task_messages(self, *, peer_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_messages WHERE peer_id = ? AND task_id = ? "
                "ORDER BY created_at, message_id",
                (peer_id, task_id),
            )
        ).fetchall()
        return [_decode_message(row) for row in rows]

    async def register_artifact(
        self,
        *,
        artifact_id: str,
        peer_id: str,
        tenant: str,
        task_id: str,
        runtime_artifact_id: str,
        title: str,
        media_type: str,
        size_bytes: int,
        sha256: str | None,
        storage_kind: str,
        inline_content: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        await self._conn.execute(
            "INSERT INTO a2a_artifacts "
            "(id, peer_id, tenant, task_id, runtime_artifact_id, title, media_type, "
            "size_bytes, sha256, storage_kind, inline_content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(peer_id, task_id, runtime_artifact_id) DO NOTHING",
            (
                artifact_id,
                peer_id,
                tenant,
                task_id,
                runtime_artifact_id,
                title,
                media_type,
                size_bytes,
                sha256,
                storage_kind,
                inline_content,
                created_at,
            ),
        )
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_artifacts "
                "WHERE peer_id = ? AND task_id = ? AND runtime_artifact_id = ?",
                (peer_id, task_id, runtime_artifact_id),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def get_artifact(
        self, *, peer_id: str, tenant: str, artifact_id: str
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_artifacts WHERE id = ? AND peer_id = ? AND tenant = ?",
                (artifact_id, peer_id, tenant),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_task_artifacts(self, *, peer_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_artifacts WHERE peer_id = ? AND task_id = ? "
                "ORDER BY created_at, id",
                (peer_id, task_id),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def create_push_config(
        self,
        *,
        config_id: str,
        peer_id: str,
        tenant: str,
        task_id: str,
        request_fingerprint: str,
        url: str,
        token_ciphertext: str | None,
        auth_scheme: str | None,
        credentials_ciphertext: str | None,
        start_after: int,
        snapshot_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        async with self._transaction() as conn:
            task = await (
                await conn.execute(
                    "SELECT 1 FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                    (task_id, peer_id, tenant),
                )
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            existing = await (
                await conn.execute(
                    "SELECT * FROM a2a_push_configs WHERE id = ? AND peer_id = ? AND task_id = ?",
                    (config_id, peer_id, task_id),
                )
            ).fetchone()
            if existing is not None:
                if (
                    existing["deleted_at"] is not None
                    or str(existing["task_id"]) != task_id
                    or str(existing["request_fingerprint"]) != request_fingerprint
                ):
                    raise A2APushConfigConflictError(
                        f"push config {config_id} already exists with different content"
                    )
                return dict(existing), False
            active_count = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM a2a_push_configs "
                    "WHERE peer_id = ? AND task_id = ? AND deleted_at IS NULL",
                    (peer_id, task_id),
                )
            ).fetchone()
            if active_count is not None and int(active_count[0]) >= 8:
                raise ValueError("a task may have at most 8 push configurations")
            now = _now()
            await conn.execute(
                "INSERT INTO a2a_push_configs "
                "(id, peer_id, tenant, task_id, request_fingerprint, url, token_ciphertext, "
                "auth_scheme, credentials_ciphertext, created_at, updated_at, deleted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    config_id,
                    peer_id,
                    tenant,
                    task_id,
                    request_fingerprint,
                    url,
                    token_ciphertext,
                    auth_scheme,
                    credentials_ciphertext,
                    now,
                    now,
                ),
            )
            await conn.execute(
                "INSERT INTO a2a_push_cursors (task_id, peer_id, runtime_after, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "runtime_after = MAX(a2a_push_cursors.runtime_after, excluded.runtime_after), "
                "updated_at = excluded.updated_at",
                (task_id, peer_id, max(0, start_after), now),
            )
            await conn.execute(
                "INSERT INTO a2a_push_outbox "
                "(id, config_id, peer_id, task_id, event_key, payload_json, status, attempts, "
                "available_at, lease_until, last_error, created_at, delivered_at) "
                "VALUES (?, ?, ?, ?, 'snapshot', ?, 'pending', 0, ?, NULL, NULL, ?, NULL)",
                (
                    f"push_{uuid.uuid4().hex}",
                    config_id,
                    peer_id,
                    task_id,
                    json.dumps(snapshot_payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            created = await (
                await conn.execute(
                    "SELECT * FROM a2a_push_configs WHERE id = ? AND peer_id = ? "
                    "AND tenant = ? AND task_id = ? AND deleted_at IS NULL",
                    (config_id, peer_id, tenant, task_id),
                )
            ).fetchone()
            assert created is not None
            return dict(created), True

    async def get_push_config(
        self, *, peer_id: str, tenant: str, task_id: str, config_id: str
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_push_configs WHERE id = ? AND peer_id = ? "
                "AND tenant = ? AND task_id = ? AND deleted_at IS NULL",
                (config_id, peer_id, tenant, task_id),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_push_configs(
        self, *, peer_id: str, tenant: str, task_id: str
    ) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_push_configs WHERE peer_id = ? AND tenant = ? "
                "AND task_id = ? AND deleted_at IS NULL ORDER BY created_at, id",
                (peer_id, tenant, task_id),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def delete_push_config(
        self, *, peer_id: str, tenant: str, task_id: str, config_id: str
    ) -> bool:
        now = _now()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE a2a_push_configs SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND peer_id = ? AND tenant = ? AND task_id = ? "
                "AND deleted_at IS NULL",
                (now, now, config_id, peer_id, tenant, task_id),
            )
            if cursor.rowcount == 1:
                await conn.execute(
                    "UPDATE a2a_push_outbox SET status = 'canceled', lease_until = NULL "
                    "WHERE config_id = ? AND peer_id = ? AND task_id = ? "
                    "AND status IN ('pending', 'leased')",
                    (config_id, peer_id, task_id),
                )
                existed = True
            else:
                existing = await (
                    await conn.execute(
                        "SELECT 1 FROM a2a_push_configs WHERE id = ? AND peer_id = ? "
                        "AND tenant = ? AND task_id = ?",
                        (config_id, peer_id, tenant, task_id),
                    )
                ).fetchone()
                existed = existing is not None
            return existed

    async def list_push_watch_tasks(self) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT t.*, c.runtime_after FROM a2a_tasks AS t "
                "JOIN a2a_push_cursors AS c ON c.task_id = t.id "
                "WHERE EXISTS (SELECT 1 FROM a2a_push_configs AS p "
                "WHERE p.task_id = t.id AND p.peer_id = t.peer_id AND p.deleted_at IS NULL) "
                "ORDER BY t.created_at, t.id"
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def record_push_event(
        self,
        *,
        peer_id: str,
        task_id: str,
        event_seq: int,
        payloads: list[dict[str, Any]],
    ) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT runtime_after FROM a2a_push_cursors WHERE task_id = ? AND peer_id = ?",
                (task_id, peer_id),
            )
            row = await cursor.fetchone()
            if row is None or event_seq <= int(row["runtime_after"]):
                return
            configs = await (
                await conn.execute(
                    "SELECT id FROM a2a_push_configs "
                    "WHERE peer_id = ? AND task_id = ? AND deleted_at IS NULL",
                    (peer_id, task_id),
                )
            ).fetchall()
            now = _now()
            for config in configs:
                for index, payload in enumerate(payloads):
                    event_key = f"event:{event_seq:020d}:{index:04d}"
                    await conn.execute(
                        "INSERT OR IGNORE INTO a2a_push_outbox "
                        "(id, config_id, peer_id, task_id, event_key, payload_json, status, "
                        "attempts, available_at, lease_until, last_error, created_at, delivered_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, NULL)",
                        (
                            f"push_{uuid.uuid4().hex}",
                            config["id"],
                            peer_id,
                            task_id,
                            event_key,
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                            now,
                            now,
                        ),
                    )
            await conn.execute(
                "UPDATE a2a_push_cursors SET runtime_after = ?, updated_at = ? "
                "WHERE task_id = ? AND peer_id = ? AND runtime_after < ?",
                (event_seq, now, task_id, peer_id, event_seq),
            )

    async def claim_push_delivery(self, *, lease_seconds: int = 30) -> dict[str, Any] | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_until = datetime.fromtimestamp(now_dt.timestamp() + lease_seconds, tz=UTC).isoformat()
        async with self._transaction() as conn:
            row = await (
                await conn.execute(
                    "SELECT o.*, p.url, p.token_ciphertext, p.auth_scheme, "
                    "p.credentials_ciphertext FROM a2a_push_outbox AS o "
                    "JOIN a2a_push_configs AS p ON p.id = o.config_id "
                    "AND p.peer_id = o.peer_id AND p.task_id = o.task_id "
                    "WHERE p.deleted_at IS NULL AND ("
                    "(o.status = 'pending' AND o.available_at <= ?) OR "
                    "(o.status = 'leased' AND o.lease_until <= ?)) "
                    "ORDER BY o.available_at, o.created_at, o.event_key, o.id LIMIT 1",
                    (now, now),
                )
            ).fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE a2a_push_outbox SET status = 'leased', attempts = attempts + 1, "
                "lease_until = ? WHERE id = ?",
                (lease_until, row["id"]),
            )
            return {**dict(row), "status": "leased", "attempts": int(row["attempts"]) + 1}

    async def settle_push_delivery(self, delivery_id: str) -> None:
        await self._conn.execute(
            "UPDATE a2a_push_outbox SET status = 'delivered', delivered_at = ?, "
            "lease_until = NULL, last_error = NULL WHERE id = ? AND status = 'leased'",
            (_now(), delivery_id),
        )

    async def retry_push_delivery(
        self, delivery_id: str, *, available_at: str, error: str, dead: bool
    ) -> None:
        await self._conn.execute(
            "UPDATE a2a_push_outbox SET status = ?, available_at = ?, lease_until = NULL, "
            "last_error = ? WHERE id = ? AND status = 'leased'",
            ("dead" if dead else "pending", available_at, error[:2048], delivery_id),
        )

    async def settle_task_admission(
        self, *, peer_id: str, task_id: str, runtime_run_id: str
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_tasks SET runtime_run_id = ?, admission_status = 'accepted', "
            "rejection_reason = NULL, updated_at = ? "
            "WHERE id = ? AND peer_id = ? AND admission_status IN ('pending', 'accepted') "
            "AND (runtime_run_id IS NULL OR runtime_run_id = ?)",
            (runtime_run_id, now, task_id, peer_id, runtime_run_id),
        )
        if cursor.rowcount != 1:
            raise A2AMessageConflictError("task admission settled with a different Runtime Run")
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ?",
                (task_id, peer_id),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def settle_message_delivery(
        self,
        *,
        peer_id: str,
        message_id: str,
        runtime_instruction_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_messages SET delivery_status = 'accepted', "
            "runtime_instruction_id = COALESCE(runtime_instruction_id, ?), "
            "rejection_reason = NULL, updated_at = ? "
            "WHERE peer_id = ? AND message_id = ? AND delivery_status IN ('pending', 'accepted') "
            "AND (runtime_instruction_id IS NULL OR runtime_instruction_id = ?)",
            (runtime_instruction_id, now, peer_id, message_id, runtime_instruction_id),
        )
        if cursor.rowcount != 1:
            raise A2AMessageConflictError("message delivery settled with a different instruction")
        message = await self.get_message(peer_id=peer_id, message_id=message_id)
        assert message is not None
        return message

    async def reject_task_admission(
        self, *, peer_id: str, task_id: str, reason: str
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_tasks SET admission_status = 'rejected', rejection_reason = ?, "
            "updated_at = ? WHERE id = ? AND peer_id = ? AND admission_status = 'pending'",
            (reason[:2048], now, task_id, peer_id),
        )
        if cursor.rowcount != 1:
            task = await (
                await self._conn.execute(
                    "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ?",
                    (task_id, peer_id),
                )
            ).fetchone()
            if task is None or task["admission_status"] != "rejected":
                raise A2AMessageConflictError("task admission could not be rejected")
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ?",
                (task_id, peer_id),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def reject_message_delivery(
        self, *, peer_id: str, message_id: str, reason: str
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_messages SET delivery_status = 'rejected', rejection_reason = ?, "
            "updated_at = ? WHERE peer_id = ? AND message_id = ? AND delivery_status = 'pending'",
            (reason[:2048], now, peer_id, message_id),
        )
        if cursor.rowcount != 1:
            message = await self.get_message(peer_id=peer_id, message_id=message_id)
            if message is None or message["delivery_status"] != "rejected":
                raise A2AMessageConflictError("message delivery could not be rejected")
        message = await self.get_message(peer_id=peer_id, message_id=message_id)
        assert message is not None
        return message
