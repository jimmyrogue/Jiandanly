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


def test_p4_stream_mirrors_the_same_envelopes_to_the_dev_trace(
    client: TestClient,
    monkeypatch,
) -> None:
    observed: list[dict] = []
    monkeypatch.setattr(
        "shejane_runtime.runs.trace_stream_event",
        lambda event: observed.append(event),
    )
    create = client.post(
        "/v1/runs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=run_command("trace this run"),
    ).json()
    run_id = create.get("id") or create.get("run", {}).get("id")

    raw = client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text
    events, _ = _parse_sse(raw)

    assert observed
    assert [event["id"] for event in observed] == [event["id"] for event in events]


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


def test_terminal_event_carries_replayable_presentation_upsert(client: TestClient) -> None:
    store = client.app.state.store

    async def seed_completed_run() -> str:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_sse_presentation",
            client_message_id="msg_sse_presentation",
            thread_id="conversation_sse_presentation",
            assistant_message_id="msg_assistant_sse_presentation",
            user_input="presentation replay",
            thread_title="SSE presentation",
            thread_metadata={},
            command_payload={"type": "run.start", "goal": "presentation replay"},
            goal="presentation replay",
            workspace_path=None,
            mode="auto",
        )
        job = await store.claim_run_job(worker_id="worker-sse-presentation")
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-sse-presentation",
            lease_generation=int(job["lease_generation"]),
        ):
            await store.append_event(
                run["id"],
                "assistant.round.committed",
                {
                    "round_id": "model-call-sse",
                    "text": "Inspecting the file.",
                    "reasoning_summary": "The repository structure determines the next read.",
                    "tool_call_ids": ["call-sse"],
                },
            )
            await store.append_event(
                run["id"],
                "tool.requested",
                {
                    "tool_call_id": "call-sse",
                    "name": "read_file",
                    "tool": "read_file",
                    "arguments": {},
                },
            )
            await store.prepare_tool_receipt(
                operation_id="toolop_sse",
                run_id=run["id"],
                execution_attempt_id=f"{job['id']}:{job['lease_generation']}",
                execution_namespace="main",
                tool_call_id="call-sse",
                tool_name="read_file",
                tool_version="builtin-v1",
                arguments_hash="sse-args",
                arguments_json="{}",
                risk="read_only",
            )
            await store.begin_tool_receipt(
                operation_id="toolop_sse",
                run_id=run["id"],
                execution_attempt_id=f"{job['id']}:{job['lease_generation']}",
            )
            await store.settle_tool_receipt(
                operation_id="toolop_sse",
                run_id=run["id"],
                status="completed",
                result_json='{"content":"done"}',
                result_hash="sse-result",
            )
            await store.append_event(
                run["id"],
                "tool.completed",
                {"tool_call_id": "call-sse", "name": "read_file", "tool": "read_file"},
            )
            await store.commit_run_result(
                run["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "Presentation result."},
            )
        return str(run["id"])

    run_id = asyncio.run(seed_completed_run())

    replayed, _done = _parse_sse(client.get(f"/v1/runs/{run_id}/stream", headers=HEADERS).text)
    terminal = next(event for event in replayed if event["event_type"] == "run.completed")

    assert terminal["presentation_change"]["kind"] == "item.upsert"
    item = terminal["presentation_change"]["item"]
    assert item["kind"] == "final_answer"
    assert item["status"] == "completed"
    assert item["content"]
    assert item["revision"] == terminal["seq"]
    changes = [event["presentation_change"] for event in replayed if "presentation_change" in event]
    assert any(
        change["kind"] == "item.upsert" and change["item"]["kind"] == "progress"
        for change in changes
    )
    round_event = next(
        event for event in replayed if event["event_type"] == "assistant.round.committed"
    )
    assert [change["item"]["kind"] for change in round_event["presentation_changes"]] == [
        "reasoning_summary",
        "progress",
    ]
    assert any(
        change["kind"] == "item.upsert"
        and change["item"]["kind"] == "tool"
        and change["item"]["status"] == "completed"
        for change in changes
    )


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

        await coordinator.emit_for_run(
            run["id"],
            "llm.delta",
            {"content": "temporary", "round_id": "model-call-live"},
        )

        event = await asyncio.wait_for(next_event, timeout=1)
        assert event["event_type"] == "llm.delta"
        assert event["payload"] == {"content": "temporary", "round_id": "model-call-live"}
        assert event["presentation_change"] == {
            "kind": "draft.delta",
            "round_id": "model-call-live",
            "content": "temporary",
        }
        assert await store.events_since(run["id"]) == []
        await stream.aclose()
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_event_type", "run_status", "expected_kinds"),
    [
        ("run.cleanup_required", "cleanup_required", {"tool", "notice"}),
        ("run.completed", "completed", {"tool"}),
    ],
)
async def test_terminal_replay_projects_completed_receipt_without_a_tool_event(
    terminal_event_type: str,
    run_status: str,
    expected_kinds: set[str],
) -> None:
    events = [
        {
            "id": "event-1",
            "run_id": "run-terminal",
            "seq": 1,
            "event_type": "tool.requested",
            "payload_json": '{"tool_call_id":"call-1","tool":"read_file"}',
            "created_at": "2026-08-04T00:00:01Z",
        },
        {
            "id": "event-2",
            "run_id": "run-terminal",
            "seq": 2,
            "event_type": terminal_event_type,
            "payload_json": "{}",
            "created_at": "2026-08-04T00:00:02Z",
        },
    ]

    class FactsStore:
        calls = 0

        async def get_run_presentation_facts(self, run_id: str) -> dict:
            assert run_id == "run-terminal"
            self.calls += 1
            return {
                "run": {"id": run_id, "status": run_status},
                "assistant_item": None,
                "events": events,
                "tool_receipts": [
                    {
                        "operation_id": "toolop-1",
                        "tool_call_id": "call-1",
                        "tool_name": "read_file",
                        "status": "completed",
                        "risk": "read_only",
                        "arguments_json": "{}",
                        "created_at": "2026-08-04T00:00:01Z",
                        "updated_at": "2026-08-04T00:00:02Z",
                        "completed_at": "2026-08-04T00:00:02Z",
                    }
                ],
                "wait_candidates": [],
                "artifacts": [],
                "event_high_watermark": 2,
            }

    store = FactsStore()
    coordinator = RunCoordinator(store, None)  # type: ignore[arg-type]

    envelopes = await coordinator._stream_event_envelopes(events)

    assert store.calls == 1
    terminal_changes = envelopes[1].get("presentation_changes") or [
        envelopes[1]["presentation_change"]
    ]
    assert {change["item"]["kind"] for change in terminal_changes} == expected_kinds
    tool = next(change["item"] for change in terminal_changes if change["item"]["kind"] == "tool")
    assert tool["id"] == "tool-call:call-1"
    assert tool["status"] == "completed"


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
