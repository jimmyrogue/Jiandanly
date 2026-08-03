from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.types.a2a_pb2 import ListTasksRequest

import shejane_runtime.a2a_gateway.outbound as outbound
from shejane_runtime.a2a_gateway.app import GatewayConfig, _agent_card, create_gateway_app
from shejane_runtime.a2a_gateway.outbound import (
    A2AOutboundError,
    A2AOutboundSecurityError,
    connect_a2a_agent,
)


@pytest.mark.asyncio
async def test_outbound_client_honors_standard_request_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = agent_card_to_dict(_agent_card("https://gateway.example.test"))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=card))
    configs: list[object] = []

    class FakeClient:
        closed = False

        async def close(self) -> None:
            self.closed = True

    fake_client = FakeClient()

    class FakeFactory:
        def __init__(self, config: object) -> None:
            configs.append(config)

        def create(self, _card: object) -> FakeClient:
            return fake_client

    monkeypatch.setattr(outbound, "ClientFactory", FakeFactory)
    connection = await connect_a2a_agent(
        "https://gateway.example.test",
        bearer_token="peer-token",
        streaming=False,
        transport_factory=lambda _url: transport,
    )
    assert configs[0].streaming is False
    await connection.close()
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_outbound_client_discovers_and_calls_the_python_oracle(
    tmp_path: Path,
) -> None:
    app = create_gateway_app(
        GatewayConfig(
            db_path=tmp_path / "gateway.db",
            runtime_base_url="http://127.0.0.1:17371",
            runtime_token="runtime-token",
            public_base_url="https://gateway.example.test",
            push_credential_key=b"k" * 32,
        )
    )
    async with app.router.lifespan_context(app):
        _peer, token = await app.state.gateway_store.create_peer(
            name="Outbound client oracle",
            tenant="outbound-oracle",
            scopes=["tasks.read"],
            runtime_model="local:test:model",
            runtime_workspace_path=None,
            permission_mode="ask",
            push_origins=[],
            expires_at=None,
        )
        connection = await connect_a2a_agent(
            "https://gateway.example.test",
            bearer_token=token,
            transport_factory=lambda _url: httpx.ASGITransport(app=app),
        )
        try:
            listed = await connection.client.list_tasks(ListTasksRequest(tenant="outbound-oracle"))
            assert list(listed.tasks) == []
            assert connection.agent_card.supported_interfaces[0].protocol_version == "1.0"
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_outbound_client_never_sends_credentials_to_cross_origin_card_endpoint() -> None:
    card = agent_card_to_dict(_agent_card("https://other.example.test"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json=card)

    with pytest.raises(A2AOutboundSecurityError, match="allowed origin"):
        await connect_a2a_agent(
            "https://discovery.example.test",
            bearer_token="secret-peer-token",
            transport_factory=lambda _url: httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_outbound_client_requires_declared_credentials() -> None:
    card = agent_card_to_dict(_agent_card("https://gateway.example.test"))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=card))

    with pytest.raises(A2AOutboundSecurityError, match="security requirements"):
        await connect_a2a_agent(
            "https://gateway.example.test",
            bearer_token=None,
            transport_factory=lambda _url: transport,
        )


@pytest.mark.asyncio
async def test_outbound_client_blocks_private_network_discovery() -> None:
    with pytest.raises(A2AOutboundError, match="blocked"):
        await connect_a2a_agent("https://127.0.0.1", bearer_token=None)
