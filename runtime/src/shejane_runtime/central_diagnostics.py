"""Opt-in, metadata-only central diagnostics relay."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .central_diagnostics_credentials import (
    delete_diagnostics_token,
    get_diagnostics_token,
    set_diagnostics_token,
)
from .model_credentials import get_model_api_key
from .model_services import openai_compatible_endpoint

log = logging.getLogger("shejane_runtime.central_diagnostics")

_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}:[0-9]{1,10}$")
_RELEASE_VERSION = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_FAILURE_CATEGORIES = {
    "auth",
    "cleanup",
    "configuration",
    "execution_cleanup_unconfirmed",
    "fatal",
    "model_output",
    "permission",
    "provider_unavailable",
    "quota",
    "transient",
    "unknown",
    "unknown_failure",
    "validation",
    "verification",
    "workspace",
}
_TOOL_NAMES = {
    "clipboard.read",
    "clipboard.write",
    "edit_file",
    "environment.observe",
    "execute",
    "glob",
    "grep",
    "image.edit",
    "image.generate",
    "ls",
    "memory.search",
    "memory.write",
    "office.add_image_to_slide",
    "office.add_row",
    "office.add_slide",
    "office.apply_style",
    "office.create_pptx",
    "office.delete_paragraph",
    "office.delete_slide",
    "office.find_replace",
    "office.insert_paragraph",
    "office.merge_cells",
    "office.outline",
    "office.read",
    "office.read_range",
    "office.read_slides",
    "office.reorder_slides",
    "office.set_cell_format",
    "office.set_cells",
    "office.set_formula",
    "office.set_slide_bullets",
    "office.set_slide_notes",
    "office.set_slide_title",
    "office.update_paragraph",
    "office.update_slide",
    "open.file",
    "open.url",
    "pdf.inspect",
    "read_file",
    "task",
    "task.progress",
    "task.verify",
    "time.now",
    "user.ask",
    "web.fetch",
    "web.search",
    "write_file",
    "write_todos",
}


class CentralDiagnosticsUnavailable(RuntimeError):
    pass


class CentralDiagnosticsConfigurationError(ValueError):
    pass


class CentralDiagnosticsManager:
    def __init__(self, *, store: Any, cloud_origin: str, app_version: str) -> None:
        parsed = urlsplit(cloud_origin)
        self._cloud_origin = (
            cloud_origin.rstrip("/")
            if parsed.scheme == "https"
            and parsed.netloc
            and not parsed.path.rstrip("/")
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            else ""
        )
        self._store = store
        self._app_version = app_version
        self._configure_lock = asyncio.Lock()
        self._renew_lock = asyncio.Lock()
        self._disable_generation = 0
        self._disable_in_progress = 0

    async def status(self, principal_id: str) -> dict[str, Any]:
        settings = await self._settings()
        enabled = settings.get("central_diagnostics_enabled") is True
        connection_id = str(settings.get("central_diagnostics_connection_id") or "")
        rate = float(settings.get("central_diagnostics_success_sample_rate") or 0)
        expires_at = int(settings.get("central_diagnostics_expires_at") or 0)
        credential_configured = bool(
            enabled
            and connection_id
            and expires_at > int(datetime.now(UTC).timestamp())
            and await get_diagnostics_token(principal_id, connection_id)
        )
        return {
            "enabled": enabled,
            "connection_id": connection_id or None,
            "success_sample_rate": rate,
            "credential_configured": credential_configured,
        }

    async def configure(
        self,
        *,
        principal_id: str,
        enabled: bool,
        connection_id: str | None,
        success_sample_rate: float,
    ) -> dict[str, Any]:
        disabling = not enabled
        if disabling:
            self._disable_generation += 1
            self._disable_in_progress += 1
        try:
            async with self._configure_lock:
                return await self._configure(
                    principal_id=principal_id,
                    enabled=enabled,
                    connection_id=connection_id,
                    success_sample_rate=success_sample_rate,
                )
        finally:
            if disabling:
                self._disable_in_progress -= 1

    async def _configure(
        self,
        *,
        principal_id: str,
        enabled: bool,
        connection_id: str | None,
        success_sample_rate: float,
    ) -> dict[str, Any]:
        if not 0 <= success_sample_rate <= 1:
            raise CentralDiagnosticsConfigurationError("invalid success sample rate")
        if not enabled:
            async with self._renew_lock:
                current = await self._settings()
                previous_connection_id = str(current.get("central_diagnostics_connection_id") or "")
                if previous_connection_id:
                    await delete_diagnostics_token(principal_id, previous_connection_id)
                await self._store.patch_runtime_settings(
                    {
                        "central_diagnostics_enabled": False,
                        "central_diagnostics_connection_id": "",
                        "central_diagnostics_success_sample_rate": 0.0,
                        "central_diagnostics_expires_at": 0,
                    },
                    initial_settings=current,
                )
            return await self.status(principal_id)

        current = await self._settings()
        previous_connection_id = str(current.get("central_diagnostics_connection_id") or "")
        if not self._cloud_origin:
            raise CentralDiagnosticsUnavailable("official SheJane Cloud origin is not configured")
        if not connection_id:
            raise CentralDiagnosticsConfigurationError("official connection is required")
        inference_token = await self._official_inference_token(principal_id, connection_id)
        diagnostics_token, expires_at = await self._mint(inference_token)
        await set_diagnostics_token(principal_id, connection_id, diagnostics_token)
        try:
            await self._store.patch_runtime_settings(
                {
                    "central_diagnostics_enabled": True,
                    "central_diagnostics_connection_id": connection_id,
                    "central_diagnostics_success_sample_rate": success_sample_rate,
                    "central_diagnostics_expires_at": expires_at,
                },
                initial_settings=current,
            )
        except BaseException:
            await delete_diagnostics_token(principal_id, connection_id)
            raise
        if previous_connection_id and previous_connection_id != connection_id:
            await delete_diagnostics_token(principal_id, previous_connection_id)
        return await self.status(principal_id)

    async def submit_terminal(
        self,
        *,
        run_id: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        disable_generation = self._disable_generation
        if self._disable_in_progress:
            return
        if status not in {"completed", "failed", "canceled", "cleanup_required"}:
            return
        settings = await self._settings()
        if settings.get("central_diagnostics_enabled") is not True:
            return
        connection_id = str(settings.get("central_diagnostics_connection_id") or "")
        if not connection_id or not self._cloud_origin:
            return
        if status == "completed":
            rate = float(settings.get("central_diagnostics_success_sample_rate") or 0)
            bucket = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big")
            if rate <= 0 or bucket / 2**64 >= rate:
                return
        run = await self._store.get_run(run_id)
        if run is None:
            return
        try:
            run_settings = json.loads(run.get("settings_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        binding = run_settings.get("_model_binding")
        if not isinstance(binding, dict) or binding.get("connection_id") != connection_id:
            return
        principal_id = str(run["principal_id"])
        token = await get_diagnostics_token(principal_id, connection_id)
        expires_at = int(settings.get("central_diagnostics_expires_at") or 0)
        if not token or expires_at <= int(datetime.now(UTC).timestamp()):
            token = await self._renew(principal_id, connection_id)
        if self._disable_in_progress or disable_generation != self._disable_generation:
            return
        event = await self._event(run, status=status, payload=payload, binding=binding)
        response = await self._post_event(token, event)
        if response.status_code == 401:
            token = await self._renew(principal_id, connection_id, stale_token=token)
            if self._disable_in_progress or disable_generation != self._disable_generation:
                return
            response = await self._post_event(token, event)
        if response.status_code != 202:
            raise CentralDiagnosticsUnavailable(
                f"diagnostics ingestion returned HTTP {response.status_code}"
            )

    async def _mint(self, inference_token: str) -> tuple[str, int]:
        try:
            async with httpx.AsyncClient(
                timeout=5,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self._cloud_origin}/api/shejane/telemetry/token",
                    headers={"Authorization": f"Bearer {inference_token}"},
                    content=b"",
                )
        except httpx.RequestError as exc:
            raise CentralDiagnosticsUnavailable("diagnostics token mint is unavailable") from exc
        if response.status_code != 201:
            raise CentralDiagnosticsUnavailable(
                f"diagnostics token mint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CentralDiagnosticsUnavailable("diagnostics token response is invalid") from exc
        token = payload.get("telemetry_token") if isinstance(payload, dict) else None
        expires_at = payload.get("expires_at") if isinstance(payload, dict) else None
        if (
            not isinstance(token, str)
            or not _canonical_diagnostics_token(token)
            or payload.get("token_type") != "Bearer"
            or not isinstance(expires_at, int)
            or expires_at <= int(datetime.now(UTC).timestamp())
        ):
            raise CentralDiagnosticsUnavailable("diagnostics token response is invalid")
        return token, expires_at

    async def _official_inference_token(
        self,
        principal_id: str,
        connection_id: str,
    ) -> str:
        connection = await self._store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if (
            connection is None
            or connection.get("preset_id") != "shejane-official"
            or connection.get("region") != "official"
            or str(connection.get("base_url") or "").rstrip("/")
            != openai_compatible_endpoint(self._cloud_origin, "").rstrip("/")
        ):
            raise CentralDiagnosticsConfigurationError("official connection is invalid")
        inference_token = await get_model_api_key(
            principal_id,
            connection_id,
            str(connection["credential_ref"]),
        )
        if not inference_token:
            raise CentralDiagnosticsConfigurationError("official connection is unavailable")
        return inference_token

    async def _renew(
        self,
        principal_id: str,
        connection_id: str,
        *,
        stale_token: str | None = None,
    ) -> str:
        async with self._renew_lock:
            settings = await self._settings()
            if (
                settings.get("central_diagnostics_enabled") is not True
                or settings.get("central_diagnostics_connection_id") != connection_id
                or self._disable_in_progress
            ):
                raise CentralDiagnosticsUnavailable("diagnostics were disabled")
            token = await get_diagnostics_token(principal_id, connection_id)
            expires_at = int(settings.get("central_diagnostics_expires_at") or 0)
            if (
                token
                and expires_at > int(datetime.now(UTC).timestamp())
                and (stale_token is None or token != stale_token)
            ):
                return token
            inference_token = await self._official_inference_token(principal_id, connection_id)
            token, expires_at = await self._mint(inference_token)
            await set_diagnostics_token(principal_id, connection_id, token)
            await self._store.patch_runtime_settings(
                {"central_diagnostics_expires_at": expires_at},
                initial_settings=settings,
            )
            return token

    async def _post_event(
        self,
        token: str,
        event: dict[str, Any],
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=2,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                return await client.post(
                    f"{self._cloud_origin}/api/shejane/telemetry/events",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=event,
                )
        except httpx.RequestError as exc:
            raise CentralDiagnosticsUnavailable("diagnostics ingestion is unavailable") from exc

    async def _settings(self) -> dict[str, Any]:
        stored = await self._store.get_runtime_settings()
        return dict(stored.get("settings") or {}) if stored else {}

    async def _event(
        self,
        run: dict[str, Any],
        *,
        status: str,
        payload: dict[str, Any],
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        started = _parse_time(str(run["created_at"]))
        ended = _parse_time(
            str(run.get("completed_at") or run.get("updated_at") or run["created_at"])
        )
        duration_ms = max(0, min(int((ended - started).total_seconds() * 1000), 604_800_000))
        receipts = await self._store.list_tool_receipts_for_run(str(run["id"]))
        tool_names = sorted(
            {
                str(receipt.get("tool_name") or "")
                for receipt in receipts
                if str(receipt.get("tool_name") or "") in _TOOL_NAMES
            }
        )[:100]
        usage = await self._store.model_usage_summary(str(run["id"]))
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"shejane-run:{run['id']}"))
        execution = payload.get("execution")
        attempt_id = str(execution.get("attempt_id") or "") if isinstance(execution, dict) else ""
        if not _safe_attempt_id(attempt_id):
            attempt_id = event_id
        model_category = str(binding.get("adapter_id") or "unknown")
        if model_category not in {
            "openai_chat",
            "openai_responses",
            "anthropic_messages",
            "google_genai",
        }:
            model_category = "unknown"
        event: dict[str, Any] = {
            "schema_version": 1,
            "event_id": event_id,
            "run_id": event_id,
            "attempt_id": attempt_id,
            "release_version": (
                self._app_version
                if len(self._app_version) <= 32 and _RELEASE_VERSION.fullmatch(self._app_version)
                else "0.0.0"
            ),
            "platform": platform_name(),
            "status": status,
            "started_at": _format_time(started),
            "ended_at": _format_time(ended),
            "duration_ms": duration_ms,
            "model_category": model_category,
            "tool_names": tool_names,
            "input_tokens": _bounded_tokens(usage.get("input_tokens")),
            "output_tokens": _bounded_tokens(usage.get("output_tokens")),
        }
        if status in {"failed", "cleanup_required"}:
            category = str(payload.get("category") or "unknown_failure")
            event["failure_category"] = (
                category if category in _FAILURE_CATEGORIES else "unknown_failure"
            )
        return event


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def _safe_attempt_id(value: str) -> bool:
    if _ATTEMPT_ID.fullmatch(value) is not None:
        return True
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _canonical_diagnostics_token(token: str) -> bool:
    if not token.startswith("st-"):
        return False
    encoded = token[3:]
    try:
        decoded = base64.b64decode(encoded + "=", altchars=b"-_", validate=True)
    except ValueError:
        return False
    return len(decoded) == 32 and base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() == encoded


def _bounded_tokens(value: Any) -> int:
    return max(0, min(int(value or 0), 1_000_000_000))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
