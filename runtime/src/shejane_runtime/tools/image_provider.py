"""OpenAI-compatible image provider transport and response validation."""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from ..config import get_settings
from ..model_services import openai_compatible_endpoint
from .web import _pinned_transport

MAX_IMAGES = 4
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_FORMAT_MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_PROVIDER_STATUS_ERRORS = {
    400: ("image_provider_invalid_request", "image provider rejected the request (400)", False),
    401: ("image_provider_unauthorized", "image provider rejected the API key", False),
    402: ("provider_quota_exceeded", "image provider quota or balance is insufficient", False),
    403: ("image_provider_permission_denied", "image provider denied access to the model", False),
    408: ("image_provider_unavailable", "image provider unavailable (408)", True),
    429: ("image_provider_rate_limited", "image provider rate limit (429)", True),
}


class ImageToolError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


async def request_generation(
    binding: dict[str, Any],
    api_key: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return await _request_provider(
        openai_compatible_endpoint(str(binding["base_url"]), "images/generations"),
        api_key,
        json_payload=payload,
    )


async def request_edit(
    binding: dict[str, Any],
    api_key: str,
    payload: dict[str, Any],
    source_path: Path,
    *,
    source_name: str | None = None,
    source_media_type: str | None = None,
) -> list[dict[str, Any]]:
    try:
        source = source_path.read_bytes()
    except OSError as exc:
        raise ImageToolError("image_source_invalid", "source image could not be read") from exc
    return await _request_provider(
        openai_compatible_endpoint(str(binding["base_url"]), "images/edits"),
        api_key,
        form_payload={key: str(value) for key, value in payload.items()},
        files={
            "image": (
                source_name or source_path.name,
                source,
                source_media_type or _media_type_for_path(source_path),
            )
        },
    )


async def _request_provider(
    url: str,
    api_key: str,
    *,
    json_payload: dict[str, Any] | None = None,
    form_payload: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().model_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                json=json_payload,
                data=form_payload,
                files=files,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc.response, api_key) from exc
    except httpx.RequestError as exc:
        raise ImageToolError(
            "image_provider_unavailable",
            "image model service network request failed",
            retryable=True,
        ) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise ImageToolError(
            "image_provider_invalid_response",
            "image model service returned invalid JSON",
        ) from exc
    raw = body.get("data") if isinstance(body, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("images") or raw.get("data") or [raw]
    if raw is None and isinstance(body, dict):
        raw = body.get("images")
    results = (
        [item for item in raw or [] if isinstance(item, dict)] if isinstance(raw, list) else []
    )
    if not results or not any(
        item.get("b64_json") or item.get("base64") or item.get("url") for item in results
    ):
        raise ImageToolError("image_provider_invalid_response", "image model returned no images")
    return results[:MAX_IMAGES]


def _provider_http_error(response: httpx.Response, api_key: str) -> ImageToolError:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    provider_error = payload.get("error") if isinstance(payload, dict) else None
    provider_code = str(
        (provider_error.get("code") if isinstance(provider_error, dict) else None)
        or (payload.get("code") if isinstance(payload, dict) else None)
        or ""
    )
    provider_message = str(
        (provider_error.get("message") if isinstance(provider_error, dict) else None)
        or (payload.get("message") if isinstance(payload, dict) else None)
        or (payload.get("msg") if isinstance(payload, dict) else None)
        or ""
    )
    provider_message = re.sub(
        r"\s+",
        " ",
        provider_message.replace(api_key, "[redacted]"),
    ).strip()[:240]
    request_id = next(
        (
            str(value)
            for value in (
                payload.get("request_id") if isinstance(payload, dict) else None,
                response.headers.get("x-request-id"),
                response.headers.get("request-id"),
            )
            if value
        ),
        None,
    )
    status = response.status_code
    suffix = f" Provider message: {provider_message}" if provider_message else ""
    if provider_code == "get_channel_failed":
        return ImageToolError(
            "image_model_unavailable",
            "image model has no available provider channel." + suffix,
            request_id=request_id,
        )
    failure = _PROVIDER_STATUS_ERRORS.get(status)
    if failure is not None:
        code, message, retryable = failure
    elif status >= 500:
        code, message, retryable = (
            "image_provider_unavailable",
            f"image provider unavailable ({status})",
            True,
        )
    else:
        code, message, retryable = (
            "image_provider_failed",
            f"image provider request failed ({status})",
            False,
        )
    return ImageToolError(
        code,
        message + suffix,
        retryable=retryable,
        request_id=request_id,
    )


async def result_bytes(result: dict[str, Any]) -> bytes:
    encoded = result.get("b64_json") or result.get("base64")
    if isinstance(encoded, str) and encoded:
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            body = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageToolError(
                "image_provider_invalid_response", "image Base64 is invalid"
            ) from exc
        if not body or len(body) > _MAX_IMAGE_BYTES:
            raise ImageToolError("image_provider_invalid_response", "image exceeds the size limit")
        return body
    url = result.get("url")
    if isinstance(url, str) and url:
        return await _download_image(url)
    raise ImageToolError("image_provider_invalid_response", "image result has no content")


async def _download_image(url: str) -> bytes:
    transport, reason = _pinned_transport(url)
    if transport is None:
        raise ImageToolError("image_download_blocked", reason)
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().model_request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        ) as client:
            async with client.stream("GET", url, headers={"Accept": "image/*"}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_IMAGE_BYTES:
                        raise ImageToolError(
                            "image_provider_invalid_response", "image exceeds the size limit"
                        )
                    body.extend(chunk)
    except httpx.HTTPError as exc:
        raise ImageToolError(
            "image_download_failed", "generated image could not be downloaded"
        ) from exc
    if not body:
        raise ImageToolError("image_provider_invalid_response", "downloaded image is empty")
    return bytes(body)


def verified_image(path: Path) -> tuple[str, str]:
    try:
        with Image.open(path) as image:
            image.verify()
            image_format = str(image.format or "").upper()
    except (OSError, UnidentifiedImageError) as exc:
        raise ImageToolError(
            "image_provider_invalid_response", "provider result is not an image"
        ) from exc
    media_type = _FORMAT_MEDIA_TYPES.get(image_format)
    if media_type is None:
        raise ImageToolError(
            "image_provider_invalid_response", "provider image format is unsupported"
        )
    return media_type, {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image_format]


def _media_type_for_path(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
