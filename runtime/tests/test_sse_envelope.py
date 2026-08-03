"""SSE wire-format contract tests.

The TypeScript client's `parseAgentSSEChunk` (sse.ts) reads
`data.event_type` and `data.payload.*` from inside the JSON body of
the `data:` line, NOT from the `event:` line. It also recognizes only
`data: [DONE]` as the completion mark — anything else leaves the
stream hung.

These tests pin the contract to the AgentRunEvent interface defined
in `runtime/sdk/src/client.ts`:

    interface AgentRunEvent {
      event_type: string
      payload?: Record<string, unknown>
      id?: string
      run_id?: string
      seq?: number
      created_at?: string
    }

Historical drift: pre-this-fix, the runtime put bare payloads in
`data:` (e.g. `data: {"content": "hi"}`), so `chunk.event_type` was
always `undefined`, every UI switch missed, and the chat showed zero
streamed text. We lock that shape here so it can't regress silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app
from shejane_runtime.store.sqlite import LocalStore
from tests.helpers import run_command


def _stream_response(events: list[tuple[str, str]]) -> httpx.Response:
    body = "".join(f"event: {n}\ndata: {p}\n\n" for n, p in events).encode("utf-8")
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _patched_async_client(handler):
    class _Patched(httpx.AsyncClient):
        def __init__(self, **kw):
            super().__init__(
                transport=httpx.MockTransport(handler),
                **{k: v for k, v in kw.items() if k != "transport"},
            )

    return _Patched


@pytest.fixture
def client(monkeypatch) -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-sse-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        # Test-only streaming model emits two text deltas then done.
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "Hello "}'),
                ("llm.delta", '{"content_delta": "world."}'),
                ("llm.done", '{"request_id": "r", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


HEADERS = {"Authorization": "Bearer tok"}


def _parse_sse(raw: str) -> tuple[list[dict], bool]:
    """Return (events_with_envelope, has_done_sentinel).

    Each event is the parsed JSON of a `data:` line; the [DONE] sentinel
    is NOT included in the event list — it's reported separately.
    """
    events: list[dict] = []
    has_done = False
    for chunk in raw.split("\n\n"):
        data_lines = [
            line[len("data:") :].strip() for line in chunk.split("\n") if line.startswith("data:")
        ]
        if not data_lines:
            continue
        body = "\n".join(data_lines)
        if body == "[DONE]":
            has_done = True
            continue
        events.append(json.loads(body))
    return events, has_done


def test_stream_emits_done_sentinel(client: TestClient) -> None:
    """`data: [DONE]` MUST be the last data frame — the TS parser keys off
    it. Without it `streamAgentSSE` never resolves the promise."""
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("say hello"),
    )
    assert create.status_code == 200, create.text
    # Tolerate both flat-LocalRun (post-Block-2) and {run: {...}} (pre).
    body = create.json()
    run_id = body.get("id") or body.get("run", {}).get("id")
    assert run_id, body

    raw = client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text
    _events, has_done = _parse_sse(raw)
    assert has_done, "missing data: [DONE] sentinel — client stream loop will hang"


def test_each_event_has_envelope_shape(client: TestClient) -> None:
    """Every event's `data:` body must be the full AgentRunEvent
    envelope, not the bare payload."""
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("say hi"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")
    raw = client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text
    events, _ = _parse_sse(raw)
    assert events, "stream emitted zero events"

    required = {"event_type", "payload", "id", "run_id", "created_at"}
    for event in events:
        missing = required - set(event.keys())
        assert not missing, f"event missing envelope keys {missing}: {event}"
        assert isinstance(event["event_type"], str) and event["event_type"]
        # payload must be a dict (not None, not bare scalar) — the client
        # reads `event.payload?.content` etc.
        assert isinstance(event["payload"], dict), event


def test_run_started_payload_carries_goal(client: TestClient) -> None:
    """Spot-check that the payload contents survive the envelope wrap —
    a bug in step 2 of the move could nest payload twice or drop it."""
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("spot-check goal text"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")
    raw = client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text
    events, _ = _parse_sse(raw)
    started = [e for e in events if e["event_type"] == "run.started"]
    assert started, "no run.started event"
    assert started[0]["payload"].get("goal") == "spot-check goal text"


def test_seq_monotonic_per_run(client: TestClient) -> None:
    """`seq` is used by the client's dedupe (App.tsx seenEventIDs) and
    must be strictly increasing per run."""
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("ordered"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")
    raw = client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text
    events, _ = _parse_sse(raw)
    seqs = [e["seq"] for e in events if e.get("seq") is not None]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "duplicate seq values"


def test_replay_after_run_completion_has_same_envelope(client: TestClient) -> None:
    """After a run completes, GET /stream replays from persistence —
    that path also has to honor the envelope contract, not the legacy
    `{event, data: <payload>}` shape."""
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("replay"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")
    # First stream — live.
    client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS)
    # Second stream — replay from store.
    raw = client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text
    events, has_done = _parse_sse(raw)
    assert has_done
    assert events
    required = {"event_type", "payload", "id", "run_id", "seq", "created_at"}
    for event in events:
        assert required <= set(event.keys()), event
    assert not {
        "llm.delta",
        "llm.round.started",
        "llm.reasoning",
        "llm.usage",
        "llm.tool_call_chunk",
    }.intersection(event["event_type"] for event in events)


@pytest.mark.asyncio
async def test_live_llm_delta_is_streamed_without_becoming_a_durable_event(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "transient-events.db")
    coordinator = RunCoordinator(store, None)  # type: ignore[arg-type]
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="stream a transient delta",
            workspace_path=None,
        )
        stream = coordinator.stream(run["id"])
        next_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)

        await coordinator.emit_for_run(run["id"], "llm.delta", {"content": "temporary"})

        event = await asyncio.wait_for(next_event, timeout=1)
        assert event["event_type"] == "llm.delta"
        assert event["payload"] == {"content": "temporary"}
        assert await store.events_since(run["id"]) == []
        await stream.aclose()
    finally:
        await store.close()


def test_stream_replays_only_events_after_the_client_cursor(client: TestClient) -> None:
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("cursor replay"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")
    first_events, _ = _parse_sse(client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text)
    durable_events = [event for event in first_events if event.get("seq") is not None]
    assert len(durable_events) >= 2
    after = int(durable_events[-2]["seq"])

    resumed_events, has_done = _parse_sse(
        client.get(f"/v1/runs/{run_id}/stream?after={after}", headers=HEADERS).text
    )

    assert has_done
    assert [event["seq"] for event in resumed_events] == [
        event["seq"] for event in durable_events if int(event["seq"]) > after
    ]


def test_stream_rejects_a_cursor_beyond_the_run_event_window(client: TestClient) -> None:
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("invalid cursor"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")
    events, _ = _parse_sse(client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text)
    latest_seq = int(events[-1]["seq"])

    response = client.get(
        f"/v1/runs/{run_id}/stream?after={latest_seq + 1}",
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "event_cursor_reset_required",
        "message": "event cursor is outside the retained event window",
        "requested_after": latest_seq + 1,
        "first_available_seq": 1,
        "latest_seq": latest_seq,
    }


def test_stream_rejects_a_cursor_behind_the_retained_event_window(client: TestClient) -> None:
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("expired cursor"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")
    client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS)
    store = client.app.state.store
    asyncio.run(store.append_event(run_id, "tool.requested", {"tool": "test"}))
    asyncio.run(store.append_event(run_id, "tool.completed", {"tool": "test"}))
    asyncio.run(
        store._conn.execute("DELETE FROM local_events WHERE run_id = ? AND seq <= 2", (run_id,))
    )
    asyncio.run(store._conn.commit())

    response = client.get(
        f"/v1/runs/{run_id}/stream?after=0",
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "event_cursor_reset_required"
    assert response.json()["detail"]["first_available_seq"] == 3
