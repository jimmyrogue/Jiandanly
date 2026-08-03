from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from shejane_runtime.a2a_gateway.app import GatewayConfig, create_gateway_app
from shejane_runtime.a2a_gateway.store import A2AGatewayStore


def _peer_input(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": "Research partner",
        "tenant": "tenant-research",
        "scopes": ["tasks.create", "tasks.read"],
        "runtime_model": "local:test:model",
        "runtime_workspace_path": None,
        "permission_mode": "ask",
        "push_origins": [],
        "expires_at": None,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_peer_tokens_are_hashed_scoped_expirable_and_revocable(tmp_path: Path) -> None:
    store = await A2AGatewayStore.open(tmp_path / "gateway.db")
    try:
        peer, token = await store.create_peer(
            name="Research partner",
            tenant="tenant-research",
            scopes=["tasks.create", "tasks.read"],
            runtime_model="local:test:model",
            runtime_workspace_path=None,
            permission_mode="ask",
            push_origins=[],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )

        assert token.startswith("sj_a2a.")
        assert token not in (tmp_path / "gateway.db").read_bytes().decode("utf-8", errors="ignore")
        authenticated = await store.authenticate_peer(token)
        assert authenticated is not None
        assert authenticated["id"] == peer["id"]
        assert authenticated["tenant"] == "tenant-research"
        assert authenticated["scopes"] == ["tasks.create", "tasks.read"]
        assert await store.authenticate_peer(token + "wrong") is None

        rotated = await store.rotate_peer_token(peer["id"])
        assert await store.authenticate_peer(token) is None
        assert (await store.authenticate_peer(rotated))["id"] == peer["id"]

        await store.revoke_peer(peer["id"])
        assert await store.authenticate_peer(rotated) is None

        _expired, expired_token = await store.create_peer(
            name="Expired partner",
            tenant="tenant-expired",
            scopes=["tasks.read"],
            runtime_model="local:test:model",
            runtime_workspace_path=None,
            permission_mode="ask",
            push_origins=[],
            expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        assert await store.authenticate_peer(expired_token) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_agent_card_is_public_but_a2a_operations_require_peer_auth(tmp_path: Path) -> None:
    app = create_gateway_app(
        GatewayConfig(
            db_path=tmp_path / "gateway.db",
            runtime_base_url="http://127.0.0.1:17371",
            runtime_token="runtime-token",
            public_base_url="http://gateway.test",
            push_credential_key=b"k" * 32,
        )
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            card_response = await client.get("/.well-known/agent-card.json")
            assert card_response.status_code == 200
            assert card_response.headers["cache-control"] == "public, max-age=300"
            assert card_response.headers["etag"]
            assert card_response.headers["last-modified"]
            card = card_response.json()
            assert card["supportedInterfaces"] == [
                {
                    "url": "http://gateway.test/a2a",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ]
            assert card["capabilities"] == {
                "streaming": True,
                "pushNotifications": True,
                "extendedAgentCard": True,
            }
            assert card["securityRequirements"] == [{"schemes": {"bearer": {}}}]
            not_modified = await client.get(
                "/.well-known/agent-card.json",
                headers={"If-None-Match": card_response.headers["etag"]},
            )
            assert not_modified.status_code == 304
            assert not_modified.content == b""
            not_modified_since = await client.get(
                "/.well-known/agent-card.json",
                headers={"If-Modified-Since": card_response.headers["last-modified"]},
            )
            assert not_modified_since.status_code == 304

            unauthorized = await client.post(
                "/a2a",
                headers={"A2A-Version": "1.0"},
                json={"jsonrpc": "2.0", "id": "1", "method": "ListTasks", "params": {}},
            )
            assert unauthorized.status_code == 401
            assert unauthorized.json() == {"error": "invalid A2A peer token"}
            assert unauthorized.headers["www-authenticate"] == 'Bearer realm="shejane-a2a"'

            peer, token = await app.state.gateway_store.create_peer(
                name="Contract client",
                tenant="contract",
                scopes=["tasks.read"],
                runtime_model="local:test:model",
                runtime_workspace_path=None,
                permission_mode="ask",
                push_origins=[],
                expires_at=None,
            )
            assert peer["tenant"] == "contract"
            wrong_content_type = await client.post(
                "/a2a",
                headers={
                    "A2A-Version": "1.0",
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain",
                },
                content=b"not json",
            )
            assert wrong_content_type.status_code == 200
            assert wrong_content_type.json()["error"]["code"] == -32005
            oversized = await client.post(
                "/a2a",
                headers={
                    "A2A-Version": "1.0",
                    "Authorization": f"Bearer {token}",
                    "Content-Length": str(1024 * 1024 + 1),
                },
                content=b"{}",
            )
            assert oversized.status_code == 413
            authenticated = await client.post(
                "/a2a",
                headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": "2",
                    "method": "ListTasks",
                    "params": {"tenant": "wrong-tenant"},
                },
            )
            assert authenticated.status_code == 200
            assert authenticated.json()["error"]["code"] == -32602

            listed = await client.post(
                "/a2a",
                headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": "3",
                    "method": "ListTasks",
                    "params": {"tenant": "contract"},
                },
            )
            assert listed.status_code == 200
            assert listed.json()["result"] == {
                "tasks": [],
                "nextPageToken": "",
                "pageSize": 0,
                "totalSize": 0,
            }
            listed_with_slash = await client.post(
                "/a2a/",
                headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": "3-slash",
                    "method": "ListTasks",
                    "params": {"tenant": "contract"},
                },
            )
            assert listed_with_slash.status_code == 200
            assert listed_with_slash.history == []
            assert listed_with_slash.json()["result"]["tasks"] == []

            for version in (None, "0.3", "1.1"):
                version_headers = {"Authorization": f"Bearer {token}"}
                if version is not None:
                    version_headers["A2A-Version"] = version
                unsupported = await client.post(
                    "/a2a",
                    headers=version_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": f"version-{version}",
                        "method": "ListTasks",
                        "params": {"tenant": "contract"},
                    },
                )
                assert unsupported.json()["error"]["code"] == -32009

            extended = await client.post(
                "/a2a",
                headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": "4",
                    "method": "GetExtendedAgentCard",
                    "params": {},
                },
            )
            assert extended.status_code == 200
            assert extended.json()["result"]["supportedInterfaces"][0]["tenant"] == "contract"


@pytest.mark.asyncio
async def test_peer_rate_limit_audit_and_trace_context_are_enforced(tmp_path: Path) -> None:
    app = create_gateway_app(
        GatewayConfig(
            db_path=tmp_path / "gateway.db",
            runtime_base_url="http://127.0.0.1:17371",
            runtime_token="runtime-token",
            public_base_url="http://gateway.test",
            push_credential_key=b"k" * 32,
            requests_per_minute=2,
        )
    )
    async with app.router.lifespan_context(app):
        peer, token = await app.state.gateway_store.create_peer(
            name="Rate limited client",
            tenant="rate-test",
            scopes=["tasks.read"],
            runtime_model="local:test:model",
            runtime_workspace_path=None,
            permission_mode="ask",
            push_origins=[],
            expires_at=None,
        )
        incoming_trace = f"00-{'1' * 32}-{'2' * 16}-01"
        headers = {
            "A2A-Version": "1.0",
            "Authorization": f"Bearer {token}",
            "traceparent": incoming_trace,
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            responses = [
                await client.post(
                    "/a2a",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": f"rate-{index}",
                        "method": "ListTasks",
                        "params": {"tenant": "rate-test"},
                    },
                )
                for index in range(3)
            ]

        assert [response.status_code for response in responses] == [200, 200, 429]
        assert responses[-1].headers["retry-after"]
        assert all(
            response.headers["traceparent"].split("-")[1] == "1" * 32 for response in responses
        )
        audit = await app.state.gateway_store.list_audit_events(peer_id=str(peer["id"]))
        assert [event["http_status"] for event in audit] == [200, 200, 429]
        assert all(event["trace_id"] == "1" * 32 for event in audit)
        assert all(event["path"] == "/a2a" for event in audit)
        assert token not in json.dumps(audit)


@pytest.mark.asyncio
async def test_agent_card_requires_mtls_when_the_listener_requires_it(tmp_path: Path) -> None:
    app = create_gateway_app(
        GatewayConfig(
            db_path=tmp_path / "gateway.db",
            runtime_base_url="http://127.0.0.1:17371",
            runtime_token="runtime-token",
            public_base_url="https://gateway.test",
            push_credential_key=b"k" * 32,
            require_mtls=True,
        )
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://gateway.test",
        ) as client:
            card = (await client.get("/.well-known/agent-card.json")).json()

    assert card["securitySchemes"]["mtls"]["mtlsSecurityScheme"]
    assert card["securityRequirements"] == [{"schemes": {"bearer": {}, "mtls": {}}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "   "}, "name"),
        ({"tenant": "../other"}, "tenant"),
        ({"scopes": []}, "scope"),
        ({"scopes": ["tasks.read", "admin.everything"]}, "scope"),
        ({"runtime_model": "auto"}, "runtime_model"),
        ({"runtime_workspace_path": "relative/path"}, "workspace"),
        ({"permission_mode": "unrestricted"}, "permission_mode"),
        ({"push_origins": ["http://callback.example.test"]}, "HTTPS"),
        ({"push_origins": ["https://localhost"]}, "public"),
        ({"push_origins": ["https://callback.example.test/path"]}, "origin"),
        ({"expires_at": "2026-08-02T10:00:00"}, "timezone"),
    ],
)
async def test_peer_profile_rejects_invalid_trust_boundary_values(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    store = await A2AGatewayStore.open(tmp_path / "gateway.db")
    try:
        with pytest.raises(ValueError, match=message):
            await store.create_peer(**_peer_input(**overrides))  # type: ignore[arg-type]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_peer_profile_is_normalized_and_tenant_is_unique(tmp_path: Path) -> None:
    store = await A2AGatewayStore.open(tmp_path / "gateway.db")
    try:
        peer, _token = await store.create_peer(
            **_peer_input(
                name="  Research partner  ",
                scopes=["tasks.read", "tasks.create", "tasks.read"],
                push_origins=[
                    "https://CALLBACK.example.test:443/",
                    "https://callback.example.test",
                ],
            )  # type: ignore[arg-type]
        )
        assert peer["name"] == "Research partner"
        assert peer["scopes"] == ["tasks.create", "tasks.read"]
        assert peer["push_origins"] == ["https://callback.example.test"]

        with pytest.raises(ValueError, match="tenant already exists"):
            await store.create_peer(
                **_peer_input(name="Duplicate", scopes=["tasks.read"])  # type: ignore[arg-type]
            )
    finally:
        await store.close()
