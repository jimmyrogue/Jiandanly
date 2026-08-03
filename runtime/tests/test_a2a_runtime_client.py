from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from shejane_runtime.a2a_gateway.runtime_client import RuntimeHTTPClient, RuntimeHTTPError
from shejane_runtime.a2a_gateway.trace_context import bind_trace


@pytest.mark.asyncio
async def test_runtime_client_uses_public_idempotent_http_contract() -> None:
    app = FastAPI()
    calls: list[tuple[str, dict[str, object]]] = []
    traces: list[tuple[str, str]] = []

    @app.middleware("http")
    async def capture_trace(request: Request, call_next):
        traces.append(
            (
                request.headers["traceparent"],
                request.headers.get("tracestate", ""),
            )
        )
        return await call_next(request)

    @app.post("/v1/runs")
    async def create_run(request: Request) -> dict[str, object]:
        assert request.headers["authorization"] == "Bearer runtime-token"
        body = await request.json()
        calls.append(("create", body))
        return {"id": "run-1", "status": "queued"}

    @app.post("/v1/runs/{run_id}/inject")
    async def inject(run_id: str, request: Request) -> dict[str, object]:
        body = await request.json()
        calls.append((f"inject:{run_id}", body))
        return {"instruction_id": "steer-1", "queued": True}

    @app.post("/v1/commands")
    async def command(request: Request) -> dict[str, object]:
        body = await request.json()
        calls.append(("command", body))
        return {"canceled": True}

    @app.get("/v1/runs/{run_id}/events")
    async def events(run_id: str, after: int) -> dict[str, object]:
        if after == 0:
            return {
                "events": [{"seq": 1, "event_type": "run.started"}],
                "has_more": True,
                "next_after": 1,
            }
        return {
            "events": [{"seq": 2, "event_type": "run.completed"}],
            "has_more": False,
            "next_after": 2,
        }

    @app.get("/v1/runs/{run_id}/stream")
    async def stream(run_id: str, after: int) -> StreamingResponse:
        async def body():
            event = json.dumps({"run_id": run_id, "seq": after + 1, "event_type": "run.started"})
            yield f"data: {event}\n\ndata: [DONE]\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    client = RuntimeHTTPClient(
        base_url="http://runtime.test",
        token="runtime-token",
        transport=httpx.ASGITransport(app=app),
    )
    try:
        parent = f"00-{'a' * 32}-{'b' * 16}-01"
        with bind_trace(parent, "vendor=value"):
            assert (await client.create_run({"command_id": "create-1"}))["id"] == "run-1"
            assert (await client.inject(run_id="run-1", command_id="inject-1", content="continue"))[
                "instruction_id"
            ] == "steer-1"
            assert (await client.cancel(run_id="run-1", command_id="cancel-1"))["canceled"]
            assert [event["seq"] for event in await client.list_events("run-1")] == [1, 2]
            streamed = [event async for event in client.stream_events(run_id="run-1", after=2)]
        assert [event["seq"] for event in streamed] == [3]
        assert all(trace.split("-")[1] == "a" * 32 for trace, _state in traces)
        assert len({trace.split("-")[2] for trace, _state in traces}) == len(traces)
        assert all(state == "vendor=value" for _trace, state in traces)
        assert calls == [
            ("create", {"command_id": "create-1"}),
            ("inject:run-1", {"command_id": "inject-1", "content": "continue"}),
            ("command", {"type": "run.cancel", "command_id": "cancel-1", "run_id": "run-1"}),
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runtime_client_preserves_retryability_and_redacts_large_errors() -> None:
    app = FastAPI()

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "x" * 5000}, status_code=503)

    client = RuntimeHTTPClient(
        base_url="http://runtime.test",
        token="runtime-token",
        transport=httpx.ASGITransport(app=app),
    )
    try:
        with pytest.raises(RuntimeHTTPError) as captured:
            await client.get_run("run-1")
        assert captured.value.status_code == 503
        assert captured.value.retryable is True
        assert len(str(captured.value)) == 2048
    finally:
        await client.close()
