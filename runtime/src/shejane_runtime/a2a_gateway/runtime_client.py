from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import httpx

from .trace_context import outbound_trace_headers


async def _inject_trace_headers(request: httpx.Request) -> None:
    request.headers.update(outbound_trace_headers())


class RuntimeHTTPError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.retryable = status_code in {408, 425, 429} or status_code >= 500


class RuntimeHTTPClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30, connect=5),
            transport=transport,
            event_hooks={"request": [_inject_trace_headers]},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            payload = response.json()
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            rendered = (
                json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail
            )
        except (ValueError, TypeError):
            rendered = response.text
        raise RuntimeHTTPError(response.status_code, rendered[:2048] or "Runtime request failed")

    async def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/v1/runs", json=payload)
        await self._raise_for_status(response)
        return response.json()

    async def inject(self, *, run_id: str, command_id: str, content: str) -> dict[str, Any]:
        response = await self._client.post(
            f"/v1/runs/{quote(run_id, safe='')}/inject",
            json={"command_id": command_id, "content": content},
        )
        await self._raise_for_status(response)
        return response.json()

    async def get_run(self, run_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/runs/{quote(run_id, safe='')}")
        await self._raise_for_status(response)
        return response.json()

    async def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/artifacts/{quote(artifact_id, safe='')}")
        await self._raise_for_status(response)
        return response.json()

    async def open_artifact_content(self, artifact_id: str) -> httpx.Response:
        request = self._client.build_request(
            "GET", f"/v1/artifacts/{quote(artifact_id, safe='')}/content"
        )
        response = await self._client.send(request, stream=True)
        if not response.is_success:
            await response.aread()
            try:
                await self._raise_for_status(response)
            finally:
                await response.aclose()
        return response

    async def cancel(self, *, run_id: str, command_id: str) -> dict[str, Any]:
        response = await self._client.post(
            "/v1/commands",
            json={"type": "run.cancel", "command_id": command_id, "run_id": run_id},
        )
        await self._raise_for_status(response)
        return response.json()

    async def get_thread_snapshot(self, thread_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/threads/{quote(thread_id, safe='')}")
        await self._raise_for_status(response)
        return response.json()

    async def list_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = after
        while True:
            response = await self._client.get(
                f"/v1/runs/{quote(run_id, safe='')}/events",
                params={"after": cursor, "limit": 5000},
            )
            await self._raise_for_status(response)
            page = response.json()
            batch = page.get("events", [])
            if not isinstance(batch, list):
                raise RuntimeHTTPError(502, "Runtime returned an invalid event page")
            events.extend(event for event in batch if isinstance(event, dict))
            if not page.get("has_more"):
                return events
            next_after = page.get("next_after")
            if not isinstance(next_after, int) or next_after <= cursor:
                raise RuntimeHTTPError(502, "Runtime event pagination did not advance")
            cursor = next_after

    async def stream_events(
        self, *, run_id: str, after: int
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with self._client.stream(
            "GET",
            f"/v1/runs/{quote(run_id, safe='')}/stream",
            params={"after": after},
            timeout=None,
        ) as response:
            if not response.is_success:
                await response.aread()
                await self._raise_for_status(response)
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    if data_lines:
                        payload = "\n".join(data_lines)
                        data_lines.clear()
                        if payload == "[DONE]":
                            return
                        try:
                            decoded = json.loads(payload)
                        except json.JSONDecodeError as exc:
                            raise RuntimeHTTPError(
                                502, "Runtime returned invalid SSE JSON"
                            ) from exc
                        if isinstance(decoded, dict):
                            yield decoded
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                payload = "\n".join(data_lines)
                if payload != "[DONE]":
                    try:
                        decoded = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise RuntimeHTTPError(502, "Runtime returned invalid SSE JSON") from exc
                    if isinstance(decoded, dict):
                        yield decoded
