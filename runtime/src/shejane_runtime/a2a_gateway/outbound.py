from __future__ import annotations

import json
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.client.card_resolver import parse_agent_card
from a2a.types.a2a_pb2 import AgentCard, AgentInterface
from google.protobuf.json_format import ParseError

from shejane_runtime.tools.web import _pinned_transport

from .trace_context import outbound_trace_headers

_CARD_PATH = "/.well-known/agent-card.json"
_MAX_CARD_BYTES = 1024 * 1024

TransportFactory = Callable[[str], httpx.AsyncBaseTransport]
SignatureVerifier = Callable[[AgentCard], None]


class A2AOutboundError(RuntimeError):
    """An outbound A2A connection could not be established."""


class A2AOutboundSecurityError(A2AOutboundError):
    """An outbound Agent Card or credential boundary is unsafe."""


@dataclass
class A2AOutboundConnection:
    agent_card: AgentCard
    client: Client

    async def close(self) -> None:
        await self.client.close()

    async def __aenter__(self) -> A2AOutboundConnection:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object | None,
    ) -> None:
        await self.close()


def _external_https_url(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise A2AOutboundSecurityError(f"{field} is invalid")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise A2AOutboundSecurityError(f"{field} is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise A2AOutboundSecurityError(
            f"{field} must be an HTTPS URL without credentials, query, or fragment"
        )
    return value.rstrip("/")


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}{f':{parsed.port}' if parsed.port not in {None, 443} else ''}"


def _network_transport(
    url: str, *, ssl_context: ssl.SSLContext | None = None
) -> httpx.AsyncBaseTransport:
    transport, reason = _pinned_transport(
        url,
        allow_fake_ip=False,
        ssl_context=ssl_context,
    )
    if transport is None:
        raise A2AOutboundError(f"outbound A2A URL was blocked: {reason}")
    return transport


async def _inject_trace(request: httpx.Request) -> None:
    request.headers.update(outbound_trace_headers())


async def _fetch_agent_card(
    url: str,
    *,
    transport_factory: TransportFactory,
) -> AgentCard:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            transport=transport_factory(url),
            headers={"Accept": "application/json", "User-Agent": "SheJane-A2A/1.0"},
            event_hooks={"request": [_inject_trace]},
        ) as client:
            async with client.stream("GET", url) as response:
                if not response.is_success:
                    raise A2AOutboundError(f"Agent Card returned HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > _MAX_CARD_BYTES:
                            raise A2AOutboundError("Agent Card exceeds the 1 MiB limit")
                    except ValueError:
                        raise A2AOutboundError("Agent Card Content-Length is invalid") from None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_CARD_BYTES:
                        raise A2AOutboundError("Agent Card exceeds the 1 MiB limit")
    except A2AOutboundError:
        raise
    except httpx.HTTPError as exc:
        raise A2AOutboundError("Agent Card request failed") from exc
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("Agent Card must be a JSON object")
        return parse_agent_card(payload)
    except (ValueError, TypeError, ParseError) as exc:
        raise A2AOutboundError("Agent Card is invalid") from exc


def _select_interface(card: AgentCard, allowed_origins: set[str]) -> AgentInterface:
    for interface in card.supported_interfaces:
        if interface.protocol_binding != "JSONRPC" or interface.protocol_version != "1.0":
            continue
        url = _external_https_url(interface.url, field="Agent Card interface URL")
        if _origin(url) not in allowed_origins:
            continue
        selected = AgentInterface()
        selected.CopyFrom(interface)
        selected.url = url
        return selected
    raise A2AOutboundSecurityError(
        "Agent Card has no A2A 1.0 JSON-RPC interface on an allowed origin"
    )


def _credential_schemes(
    card: AgentCard,
    *,
    bearer_token: str | None,
    client_certificate: bool,
) -> set[str]:
    provided: set[str] = set()
    for name, scheme in card.security_schemes.items():
        kind = scheme.WhichOneof("scheme")
        if client_certificate and kind == "mtls_security_scheme":
            provided.add(name)
        if bearer_token is None:
            continue
        if kind in {"oauth2_security_scheme", "open_id_connect_security_scheme"}:
            provided.add(name)
        elif (
            kind == "http_auth_security_scheme"
            and scheme.http_auth_security_scheme.scheme.lower() == "bearer"
        ):
            provided.add(name)
    return provided


def _require_supported_security(
    card: AgentCard,
    *,
    bearer_token: str | None,
    client_certificate: bool,
) -> None:
    if not card.security_requirements:
        return
    provided = _credential_schemes(
        card,
        bearer_token=bearer_token,
        client_certificate=client_certificate,
    )
    if any(set(requirement.schemes) <= provided for requirement in card.security_requirements):
        return
    raise A2AOutboundSecurityError(
        "configured credentials do not satisfy the Agent Card security requirements"
    )


async def connect_a2a_agent(
    base_url: str,
    *,
    bearer_token: str | None,
    streaming: bool = True,
    allowed_origins: set[str] | None = None,
    client_certificate: bool = False,
    ssl_context: ssl.SSLContext | None = None,
    require_signed_card: bool = False,
    signature_verifier: SignatureVerifier | None = None,
    transport_factory: TransportFactory | None = None,
) -> A2AOutboundConnection:
    base = _external_https_url(base_url, field="A2A discovery URL")
    normalized_origins = {_origin(base)}
    for value in allowed_origins or set():
        origin_url = _external_https_url(value, field="allowed A2A origin")
        if urlsplit(origin_url).path not in {"", "/"}:
            raise A2AOutboundSecurityError("allowed A2A origin must not contain a path")
        normalized_origins.add(_origin(origin_url))
    if bearer_token is not None and (
        not bearer_token
        or len(bearer_token.encode("utf-8")) > 16 * 1024
        or any(ord(character) < 0x20 for character in bearer_token)
    ):
        raise A2AOutboundSecurityError("A2A bearer token is invalid")
    if ssl_context is not None and transport_factory is not None:
        raise A2AOutboundSecurityError(
            "ssl_context and a custom transport factory cannot be used together"
        )
    if client_certificate and ssl_context is None and transport_factory is None:
        raise A2AOutboundSecurityError(
            "client_certificate requires an mTLS SSL context or custom transport"
        )
    client_certificate = client_certificate or ssl_context is not None
    factory = transport_factory or (lambda url: _network_transport(url, ssl_context=ssl_context))
    card = await _fetch_agent_card(f"{base}{_CARD_PATH}", transport_factory=factory)
    if require_signed_card and (not card.signatures or signature_verifier is None):
        raise A2AOutboundSecurityError("a signed Agent Card and verifier are required")
    if signature_verifier is not None:
        try:
            signature_verifier(card)
        except Exception as exc:
            raise A2AOutboundSecurityError("Agent Card signature verification failed") from exc
    interface = _select_interface(card, normalized_origins)
    _require_supported_security(
        card,
        bearer_token=bearer_token,
        client_certificate=client_certificate,
    )

    selected_card = AgentCard()
    selected_card.CopyFrom(card)
    selected_card.ClearField("supported_interfaces")
    selected_card.supported_interfaces.add().CopyFrom(interface)
    headers = {"User-Agent": "SheJane-A2A/1.0"}
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=5),
        follow_redirects=False,
        transport=factory(interface.url),
        headers=headers,
        event_hooks={"request": [_inject_trace]},
    )
    try:
        client = ClientFactory(
            ClientConfig(
                streaming=streaming,
                httpx_client=http_client,
                supported_protocol_bindings=["JSONRPC"],
            )
        ).create(selected_card)
    except Exception:
        await http_client.aclose()
        raise
    return A2AOutboundConnection(agent_card=selected_card, client=client)
