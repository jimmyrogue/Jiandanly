from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import jwt

from shejane_runtime.tools.web import _pinned_transport

_ALLOWED_ALGORITHMS = frozenset({"RS256", "PS256", "ES256", "EdDSA"})
_MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_TOKEN_BYTES = 16 * 1024
_CACHE_SECONDS = 300
_MIN_REFRESH_SECONDS = 30

FetchJSON = Callable[[str], Awaitable[dict[str, object]]]


class OIDCUnavailableError(RuntimeError):
    """The configured identity provider cannot currently validate tokens."""


def validate_oidc_url(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field} must be an HTTPS URL without credentials, query, or fragment")
    return value


def validate_oidc_configuration(
    *,
    issuer: str | None,
    discovery_url: str | None,
    audience: str | None,
) -> tuple[str, str, str] | None:
    values = (issuer, discovery_url, audience)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("OIDC issuer, discovery URL, and audience must be configured together")
    assert issuer is not None and discovery_url is not None and audience is not None
    normalized_issuer = validate_oidc_url(issuer, field="OIDC issuer")
    normalized_discovery = validate_oidc_url(discovery_url, field="OIDC discovery URL")
    if (
        not audience
        or len(audience) > 512
        or audience != audience.strip()
        or any(ord(character) < 0x20 for character in audience)
    ):
        raise ValueError("OIDC audience must contain 1 to 512 valid characters")
    return normalized_issuer, normalized_discovery, audience


async def _fetch_json(url: str) -> dict[str, object]:
    validate_oidc_url(url, field="OIDC metadata URL")
    transport, reason = _pinned_transport(url, allow_fake_ip=False)
    if transport is None:
        raise OIDCUnavailableError(f"OIDC metadata URL was blocked: {reason}")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json, application/jwk-set+json"},
        ) as client:
            async with client.stream("GET", url) as response:
                if not response.is_success:
                    raise OIDCUnavailableError(
                        f"OIDC metadata returned HTTP {response.status_code}"
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > _MAX_DOCUMENT_BYTES:
                            raise OIDCUnavailableError("OIDC metadata is too large")
                    except ValueError:
                        raise OIDCUnavailableError(
                            "OIDC metadata Content-Length is invalid"
                        ) from None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_DOCUMENT_BYTES:
                        raise OIDCUnavailableError("OIDC metadata is too large")
    except OIDCUnavailableError:
        raise
    except httpx.HTTPError as exc:
        raise OIDCUnavailableError("OIDC metadata request failed") from exc
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OIDCUnavailableError("OIDC metadata is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OIDCUnavailableError("OIDC metadata must be a JSON object")
    return payload


class OIDCAuthenticator:
    def __init__(
        self,
        *,
        issuer: str,
        discovery_url: str,
        audience: str,
        fetch_json: FetchJSON | None = None,
    ) -> None:
        validated = validate_oidc_configuration(
            issuer=issuer,
            discovery_url=discovery_url,
            audience=audience,
        )
        assert validated is not None
        self.issuer, self.discovery_url, self.audience = validated
        self._fetch_json = fetch_json or _fetch_json
        self._keys: dict[tuple[str, str], jwt.PyJWK] = {}
        self._loaded_at = 0.0
        self._load_lock = asyncio.Lock()

    async def authenticate(self, token: str) -> tuple[str, str] | None:
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
            or token.count(".") != 2
        ):
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if (
            not isinstance(algorithm, str)
            or algorithm not in _ALLOWED_ALGORITHMS
            or not isinstance(key_id, str)
            or not key_id
            or len(key_id) > 256
        ):
            return None

        key = await self._get_key(key_id, algorithm)
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                audience=self.audience,
                issuer=self.issuer,
                leeway=30,
                options={"require": ["iss", "sub", "aud", "exp"]},
            )
        except jwt.PyJWTError:
            return None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 512 or "\x00" in subject:
            return None
        return self.issuer, subject

    async def _get_key(self, key_id: str, algorithm: str) -> jwt.PyJWK | None:
        await self._load_keys(force=False)
        key = self._keys.get((key_id, algorithm))
        if key is not None:
            return key
        await self._load_keys(force=True)
        return self._keys.get((key_id, algorithm))

    async def _load_keys(self, *, force: bool) -> None:
        async with self._load_lock:
            now = time.monotonic()
            if not force and self._keys and now - self._loaded_at < _CACHE_SECONDS:
                return
            if force and self._keys and now - self._loaded_at < _MIN_REFRESH_SECONDS:
                return
            discovery = await self._fetch_json(self.discovery_url)
            if discovery.get("issuer") != self.issuer:
                raise OIDCUnavailableError("OIDC discovery issuer does not match configuration")
            jwks_url = discovery.get("jwks_uri")
            if not isinstance(jwks_url, str):
                raise OIDCUnavailableError("OIDC discovery is missing jwks_uri")
            try:
                validate_oidc_url(jwks_url, field="OIDC JWKS URL")
            except ValueError as exc:
                raise OIDCUnavailableError(str(exc)) from exc
            jwks = await self._fetch_json(jwks_url)
            raw_keys = jwks.get("keys")
            if not isinstance(raw_keys, list) or len(raw_keys) > 100:
                raise OIDCUnavailableError("OIDC JWKS must contain at most 100 keys")
            keys: dict[tuple[str, str], jwt.PyJWK] = {}
            for raw_key in raw_keys:
                if not isinstance(raw_key, dict):
                    continue
                key_id = raw_key.get("kid")
                algorithm = raw_key.get("alg")
                if (
                    not isinstance(key_id, str)
                    or not key_id
                    or len(key_id) > 256
                    or not isinstance(algorithm, str)
                    or algorithm not in _ALLOWED_ALGORITHMS
                    or raw_key.get("use") not in {None, "sig"}
                ):
                    continue
                key_ops = raw_key.get("key_ops")
                if key_ops is not None and (
                    not isinstance(key_ops, list) or "verify" not in key_ops
                ):
                    continue
                identity = (key_id, algorithm)
                if identity in keys:
                    raise OIDCUnavailableError("OIDC JWKS contains a duplicate signing key")
                try:
                    key = jwt.PyJWK.from_dict(raw_key, algorithm=algorithm)
                except (jwt.PyJWTError, ValueError, TypeError) as exc:
                    raise OIDCUnavailableError("OIDC JWKS contains an invalid signing key") from exc
                keys[identity] = key
            if not keys:
                raise OIDCUnavailableError("OIDC JWKS contains no supported signing keys")
            self._keys = keys
            self._loaded_at = now
