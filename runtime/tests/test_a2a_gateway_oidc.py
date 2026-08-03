from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shejane_runtime.a2a_gateway.app import GatewayConfig, create_gateway_app
from shejane_runtime.a2a_gateway.oidc import (
    OIDCAuthenticator,
    OIDCUnavailableError,
    _fetch_json,
)
from shejane_runtime.a2a_gateway.store import A2AGatewayStore

_ISSUER = "https://identity.example.test"
_DISCOVERY_URL = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL = f"{_ISSUER}/jwks"
_AUDIENCE = "shejane-a2a"


def _key_material() -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "signing-key-1", "alg": "RS256", "use": "sig"})
    return private_key, jwk


def _token(private_key: object, *, subject: str = "partner-42", **claims: object) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "iss": _ISSUER,
        "sub": subject,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key-1", "typ": "at+jwt"},
    )


def _authenticator(private_key: object) -> OIDCAuthenticator:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "signing-key-1", "alg": "RS256", "use": "sig"})

    async def fetch_json(url: str) -> dict[str, object]:
        if url == _DISCOVERY_URL:
            return {"issuer": _ISSUER, "jwks_uri": _JWKS_URL}
        if url == _JWKS_URL:
            return {"keys": [jwk]}
        raise AssertionError(f"unexpected OIDC URL: {url}")

    return OIDCAuthenticator(
        issuer=_ISSUER,
        discovery_url=_DISCOVERY_URL,
        audience=_AUDIENCE,
        fetch_json=fetch_json,
    )


@pytest.mark.asyncio
async def test_oidc_authenticator_validates_issuer_audience_expiry_and_algorithm() -> None:
    private_key, _jwk = _key_material()
    authenticator = _authenticator(private_key)

    assert await authenticator.authenticate(_token(private_key)) == (
        _ISSUER,
        "partner-42",
    )
    assert await authenticator.authenticate(_token(private_key, aud="another-service")) is None
    assert (
        await authenticator.authenticate(
            _token(private_key, exp=datetime.now(UTC) - timedelta(minutes=1))
        )
        is None
    )
    assert (
        await authenticator.authenticate(
            jwt.encode(
                {
                    "iss": _ISSUER,
                    "sub": "partner-42",
                    "aud": _AUDIENCE,
                    "exp": datetime.now(UTC) + timedelta(minutes=5),
                },
                "shared-secret-that-is-at-least-32-bytes",
                algorithm="HS256",
                headers={"kid": "signing-key-1"},
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_oidc_peer_maps_to_database_owned_scope_and_agent_card(tmp_path: Path) -> None:
    private_key, _jwk = _key_material()
    app = create_gateway_app(
        GatewayConfig(
            db_path=tmp_path / "gateway.db",
            runtime_base_url="http://127.0.0.1:17371",
            runtime_token="runtime-token",
            public_base_url="https://gateway.example.test",
            push_credential_key=b"k" * 32,
            oidc_issuer=_ISSUER,
            oidc_discovery_url=_DISCOVERY_URL,
            oidc_audience=_AUDIENCE,
        ),
        oidc_authenticator=_authenticator(private_key),
    )
    async with app.router.lifespan_context(app):
        peer, _opaque_token = await app.state.gateway_store.create_peer(
            name="OIDC partner",
            tenant="oidc-partner",
            scopes=["tasks.read"],
            runtime_model="local:test:model",
            runtime_workspace_path=None,
            permission_mode="ask",
            push_origins=[],
            expires_at=None,
            oidc_issuer=_ISSUER,
            oidc_subject="partner-42",
        )
        assert peer["oidc_subject"] == "partner-42"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://gateway.example.test"
        ) as client:
            card = (await client.get("/.well-known/agent-card.json")).json()
            assert card["securitySchemes"]["oidc"]["openIdConnectSecurityScheme"] == {
                "description": "OIDC-issued OAuth 2.0 JWT access token.",
                "openIdConnectUrl": _DISCOVERY_URL,
            }
            assert card["securityRequirements"] == [
                {"schemes": {"bearer": {}}},
                {"schemes": {"oidc": {}}},
            ]

            response = await client.post(
                "/a2a",
                headers={
                    "A2A-Version": "1.0",
                    "Authorization": f"Bearer {_token(private_key)}",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "oidc-list",
                    "method": "ListTasks",
                    "params": {"tenant": "oidc-partner"},
                },
            )
            assert response.status_code == 200
            assert response.json()["result"]["tasks"] == []


@pytest.mark.asyncio
async def test_oidc_metadata_outage_is_retryable_not_an_invalid_token(
    tmp_path: Path,
) -> None:
    async def unavailable(_url: str) -> dict[str, object]:
        raise OIDCUnavailableError("OIDC metadata is unavailable")

    authenticator = OIDCAuthenticator(
        issuer=_ISSUER,
        discovery_url=_DISCOVERY_URL,
        audience=_AUDIENCE,
        fetch_json=unavailable,
    )
    app = create_gateway_app(
        GatewayConfig(
            db_path=tmp_path / "gateway.db",
            runtime_base_url="http://127.0.0.1:17371",
            runtime_token="runtime-token",
            public_base_url="https://gateway.example.test",
            push_credential_key=b"k" * 32,
            oidc_issuer=_ISSUER,
            oidc_discovery_url=_DISCOVERY_URL,
            oidc_audience=_AUDIENCE,
        ),
        oidc_authenticator=authenticator,
    )
    private_key, _jwk = _key_material()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://gateway.example.test"
        ) as client:
            response = await client.post(
                "/a2a",
                headers={
                    "A2A-Version": "1.0",
                    "Authorization": f"Bearer {_token(private_key)}",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "oidc-unavailable",
                    "method": "ListTasks",
                    "params": {},
                },
            )
            assert response.status_code == 503
            assert response.headers["retry-after"] == "30"


@pytest.mark.asyncio
async def test_oidc_peer_identity_migrates_and_remains_unique(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE a2a_peers ("
        "id TEXT PRIMARY KEY, principal_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
        "tenant TEXT NOT NULL UNIQUE, token_digest TEXT NOT NULL, scopes_json TEXT NOT NULL, "
        "runtime_model TEXT NOT NULL, runtime_workspace_path TEXT, permission_mode TEXT NOT NULL, "
        "push_origins_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "expires_at TEXT, revoked_at TEXT, last_used_at TEXT)"
    )
    connection.close()

    store = await A2AGatewayStore.open(db_path)
    try:
        peer, _token = await store.create_peer(
            name="Migrated OIDC peer",
            tenant="migrated-oidc",
            scopes=["tasks.read"],
            runtime_model="local:test:model",
            runtime_workspace_path=None,
            permission_mode="ask",
            push_origins=[],
            expires_at=None,
            oidc_issuer=_ISSUER,
            oidc_subject="partner-42",
        )
        authenticated = await store.authenticate_oidc_peer(issuer=_ISSUER, subject="partner-42")
        assert authenticated is not None
        assert authenticated["id"] == peer["id"]

        with pytest.raises(ValueError, match="already belongs"):
            await store.create_peer(
                name="Duplicate OIDC peer",
                tenant="duplicate-oidc",
                scopes=["tasks.read"],
                runtime_model="local:test:model",
                runtime_workspace_path=None,
                permission_mode="ask",
                push_origins=[],
                expires_at=None,
                oidc_issuer=_ISSUER,
                oidc_subject="partner-42",
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_oidc_metadata_fetch_blocks_private_networks() -> None:
    with pytest.raises(OIDCUnavailableError, match="blocked"):
        await _fetch_json("https://127.0.0.1/.well-known/openid-configuration")
