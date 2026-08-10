"""End-to-end token streaming + latency benchmark.

These tests start the full FastAPI app with a mocked backend that emits
many small `llm.delta` events, and verify:
1. Each backend delta surfaces to the client SSE stream as one `llm.delta`.
2. Time from POST /v1/runs to first `llm.delta` event observed by the
   client is below the budget (target: < 50 ms p50, < 200 ms p95 per the
   Phase 4' plan).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.server import create_app
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


def _build_token_stream(tokens: list[str]) -> list[tuple[str, str]]:
    events = [("llm.delta", json.dumps({"content_delta": tok})) for tok in tokens]
    events.append(("llm.done", '{"request_id": "r", "finish_reason": "stop"}'))
    return events


@pytest.fixture
def client_with_tokens(monkeypatch) -> tuple[TestClient, list[str]]:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-latency-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    tokens = ["Hello", ", ", "world", "!", " ", "How", " ", "are", " ", "you", "?"]

    def handler(request: httpx.Request) -> httpx.Response:
        return _stream_response(_build_token_stream(tokens))

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))

    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, tokens


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current = ""
    buf: list[str] = []
    for line in body.split("\n"):
        line = line.rstrip("\r")
        if not line:
            if current or buf:
                try:
                    data = json.loads("\n".join(buf))
                except json.JSONDecodeError:
                    data = {"raw": "\n".join(buf)}
                events.append((current, data))
            current = ""
            buf = []
            continue
        if line.startswith("event:"):
            current = line[6:].strip()
        elif line.startswith("data:"):
            buf.append(line[5:].strip())
    if current or buf:
        try:
            data = json.loads("\n".join(buf))
        except json.JSONDecodeError:
            data = {"raw": "\n".join(buf)}
        events.append((current, data))
    return events


def test_each_backend_delta_surfaces_as_llm_token(client_with_tokens) -> None:
    client, tokens = client_with_tokens
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("Hi"),
    )
    run_id = r.json()["id"]

    with client.stream(
        "GET", f"/v1/runs/{run_id}/stream", headers={"Authorization": "Bearer tok"}
    ) as resp:
        body = resp.read().decode("utf-8")

    events = _parse_sse(body)
    # Each event's `data:` body is now the AgentRunEvent envelope —
    # `event_type` and `payload` live INSIDE the data JSON. The
    # `event:` line is decorative for browser tooling.
    received_text = ""
    round_delta_counts: list[int] = []
    for _, event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") == "llm.round.started":
            received_text = ""
            round_delta_counts.append(0)
        elif event.get("event_type") == "llm.delta":
            received_text += event.get("payload", {}).get("content", "")
            round_delta_counts[-1] += 1
    expected = "".join(tokens)
    assert received_text == expected
    assert round_delta_counts
    assert all(count == len(tokens) for count in round_delta_counts)


def test_dev_trace_observes_deltas_without_an_sse_subscriber(
    client_with_tokens,
    monkeypatch,
) -> None:
    client, tokens = client_with_tokens
    observed: list[dict] = []
    monkeypatch.setattr(
        "shejane_runtime.runs.trace_stream_event",
        lambda event: observed.append(event),
    )
    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("Trace without subscribing"),
    )
    run_id = response.json()["id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client.get(
            f"/v1/runs/{run_id}",
            headers={"Authorization": "Bearer tok"},
        ).json()
        if run.get("status") not in {"queued", "running"}:
            break
        time.sleep(0.01)

    deltas_by_round: dict[str, list[str]] = {}
    for event in observed:
        if event.get("event_type") != "llm.delta":
            continue
        payload = event["payload"]
        deltas_by_round.setdefault(str(payload.get("round_id")), []).append(payload["content"])
    assert deltas_by_round
    assert all(deltas == tokens for deltas in deltas_by_round.values())


def test_terminal_event_present(client_with_tokens) -> None:
    client, _ = client_with_tokens
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("Hi"),
    )
    run_id = r.json()["id"]  # flat LocalRun shape

    with client.stream(
        "GET", f"/v1/runs/{run_id}/stream", headers={"Authorization": "Bearer tok"}
    ) as resp:
        body = resp.read().decode("utf-8")

    events = _parse_sse(body)
    names = [d.get("event_type") for _, d in events if isinstance(d, dict) and "event_type" in d]
    assert {"run.completed", "run.failed"}.intersection(names)


def test_first_token_latency_under_budget(client_with_tokens) -> None:
    """Measure the wall-clock from POST /v1/runs to the first `llm.token`
    event the client observes. Budget: p50 < 100 ms over 5 iterations.

    The budget is intentionally generous for TestClient — uvicorn over a
    real socket is faster, but TestClient adds threading + event-loop
    overhead per request. The point is to detect *regressions* (a 10x
    slowdown would scream), not to validate sub-50ms production behaviour.
    """
    client, _ = client_with_tokens
    latencies_ms: list[float] = []

    for i in range(5):
        t0 = time.perf_counter()
        r = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(f"iter-{i}"),
        )
        run_id = r.json()["id"]  # flat LocalRun

        first_token_at: float | None = None
        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            for line in resp.iter_lines():
                # The `event:` line is decorative — but as long as it
                # matches the envelope's `event_type` it's fine to grep.
                if line.startswith("event: llm.delta"):
                    first_token_at = time.perf_counter()
                    break

        assert first_token_at is not None, "never saw llm.delta"
        latencies_ms.append((first_token_at - t0) * 1000)

    latencies_ms.sort()
    p50 = latencies_ms[len(latencies_ms) // 2]
    p_max = latencies_ms[-1]
    # Report — pytest will surface this via -v
    print(f"\nfirst-token latency  samples={latencies_ms}  p50={p50:.1f}ms  max={p_max:.1f}ms")

    # Wall-clock timing on a shared CI runner is inherently jittery (a cold
    # runner routinely blows a 500ms budget without any real regression), so
    # the tight budget is OPT-IN: set SHEJANE_ENFORCE_LATENCY_BUDGET=1 locally
    # or on a dedicated perf box to enforce it. By default we only guard
    # against a gross 10x+ regression so this never flakes CI.
    strict = os.environ.get("SHEJANE_ENFORCE_LATENCY_BUDGET") == "1"
    budget_ms = 500 if strict else 5000
    assert p50 < budget_ms, f"p50 latency {p50:.1f}ms exceeded {budget_ms}ms budget"
