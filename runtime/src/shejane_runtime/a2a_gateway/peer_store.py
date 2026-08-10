"""A2A peer identity, authentication, rate limiting, and audit state."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

from .store_common import _now

_TOKEN_PREFIX = "sj_a2a"
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUNTIME_MODEL_RE = re.compile(r"^local:[^:\s]+:\S+$")
_ALLOWED_SCOPES = frozenset({"tasks.create", "tasks.read", "tasks.cancel", "push.manage"})
_PERMISSION_MODES = frozenset({"ask", "auto", "full_access"})


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


class A2APeerStore:
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
