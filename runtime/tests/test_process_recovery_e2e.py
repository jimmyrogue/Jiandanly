from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.resilience,
    pytest.mark.flow_p7_resume,
    pytest.mark.flow_p10_tool_hitl,
    pytest.mark.skipif(
        os.environ.get("SHEJANE_RUN_PROCESS_E2E") != "1",
        reason="real-process recovery runs only in make test-e2e",
    ),
]


def test_waiting_permission_resumes_after_runtime_restart(tmp_path: Path) -> None:
    port = _free_port()
    token = "process-recovery-token"
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    headers = {"Authorization": f"Bearer {token}"}

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            authorized = client.post(
                "/v1/workspaces",
                json={"path": str(workspace), "label": "process recovery"},
            )
            assert authorized.status_code == 200, authorized.text
            accepted = client.post(
                "/v1/runs",
                json={
                    "command_id": "cmd_process_recovery_run",
                    "client_message_id": "msg_process_recovery_run",
                    "protocol_version": 1,
                    "required_capabilities": [
                        "agent.run",
                        "agent.stream",
                        "hitl",
                        "subagents",
                        "workspace.files",
                    ],
                    "goal": "[[e2e:write-file]] survive a Runtime restart",
                    "workspace_path": str(workspace),
                    "model": "local:test:model",
                    "settings": {"memory": "off", "skills": "off", "mcp": "off"},
                },
            )
            assert accepted.status_code == 200, accepted.text
            run_id = str(accepted.json()["id"])
            first = _stream_events(client, run_id)
            permission = next(
                event["payload"] for event in first if event["event_type"] == "permission.required"
            )
            assert any(event["event_type"] == "run.waiting" for event in first)
            assert not (workspace / "approved.txt").exists()

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            resolved = client.post(
                "/v1/commands",
                json={
                    "type": "permission.resolve",
                    "command_id": "cmd_process_recovery_approve",
                    "permission_id": permission["request_id"],
                    "decision": "approve",
                    "scope": "once",
                },
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["resumed"] is True
            second = _stream_events(client, run_id)
            assert sum(event["event_type"] == "run.completed" for event in second) == 1
            assert (workspace / "approved.txt").read_text(encoding="utf-8") == "approved by E2E"

            diagnostics = client.get(f"/v1/runs/{run_id}/diagnostics")
            assert diagnostics.status_code == 200, diagnostics.text
            receipts = [
                receipt
                for receipt in diagnostics.json()["tool_receipts"]
                if receipt["tool_name"] == "write_file"
            ]
            assert len(receipts) == 1
            assert receipts[0]["status"] == "completed"
            assert receipts[0]["attempt_count"] == 1


def test_runtime_killed_during_sandboxed_command_quarantines_without_replay(
    tmp_path: Path,
) -> None:
    port = _free_port()
    token = "process-killpoint-token"
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace-killpoint"
    workspace.mkdir()
    headers = {"Authorization": f"Bearer {token}"}
    marker = f"shejane-process-kill-{time.time_ns()}"
    command = f"/bin/sh -c 'sleep 30' {marker}"
    goal = _encoded_tool_goal("execute", {"command": command})

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir) as process:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            authorized = client.post(
                "/v1/workspaces",
                json={"path": str(workspace), "label": "process killpoint"},
            )
            assert authorized.status_code == 200, authorized.text
            accepted = client.post(
                "/v1/runs",
                json={
                    "command_id": "cmd_process_killpoint_run",
                    "client_message_id": "msg_process_killpoint_run",
                    "protocol_version": 1,
                    "required_capabilities": [
                        "agent.run",
                        "agent.stream",
                        "hitl",
                        "subagents",
                        "workspace.files",
                    ],
                    "goal": goal,
                    "workspace_path": str(workspace),
                    "model": "local:test:model",
                    "settings": {"memory": "off", "skills": "off", "mcp": "off"},
                },
            )
            assert accepted.status_code == 200, accepted.text
            run_id = str(accepted.json()["id"])
            first = _stream_events(client, run_id)
            permission = next(
                event["payload"] for event in first if event["event_type"] == "permission.required"
            )
            resolved = client.post(
                "/v1/commands",
                json={
                    "type": "permission.resolve",
                    "command_id": "cmd_process_killpoint_approve",
                    "permission_id": permission["request_id"],
                    "decision": "approve",
                    "scope": "once",
                },
            )
            assert resolved.status_code == 200, resolved.text
            child_pids = _wait_for_process_marker(marker, process)
            process.kill()
            process.wait(timeout=10)

    for child_pid in child_pids:
        _wait_for_process_exit(child_pid)

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            events = _stream_events(client, run_id, timeout=45)
            cleanup = next(
                event for event in events if event["event_type"] == "run.cleanup_required"
            )
            assert cleanup["payload"]["category"] == "execution_lease_expired"
            assert cleanup["payload"]["retryable"] is False
            diagnostics = client.get(f"/v1/runs/{run_id}/diagnostics")
            assert diagnostics.status_code == 200, diagnostics.text
            receipts = [
                receipt
                for receipt in diagnostics.json()["tool_receipts"]
                if receipt["tool_name"] == "execute"
            ]
            assert len(receipts) == 1
            assert receipts[0]["status"] == "outcome_unknown"
            assert receipts[0]["error_type"] == "execution_lease_expired"
            assert receipts[0]["attempt_count"] == 1


def test_inflight_model_stream_converges_to_cleanup_required_after_restart(
    tmp_path: Path,
) -> None:
    port = _free_port()
    token = "process-stream-recovery-token"
    data_dir = tmp_path / "data"
    thread_id = "thread_process_stream_recovery"
    headers = {"Authorization": f"Bearer {token}"}

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir) as process:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            accepted = client.post(
                "/v1/runs",
                json={
                    "command_id": "cmd_process_stream_recovery_run",
                    "client_message_id": "msg_process_stream_recovery_run",
                    "protocol_version": 1,
                    "required_capabilities": [
                        "agent.run",
                        "agent.stream",
                        "hitl",
                        "subagents",
                        "workspace.files",
                    ],
                    "goal": "[[e2e:slow]] recover the in-flight model stream",
                    "thread_id": thread_id,
                    "model": "local:test:model",
                    "settings": {"memory": "off", "skills": "off", "mcp": "off"},
                },
            )
            assert accepted.status_code == 200, accepted.text
            run_id = str(accepted.json()["id"])
            first = _stream_events(client, run_id, stop_after="llm.delta")
            assert any(event["event_type"] == "llm.delta" for event in first)
            cursor = max(int(event["seq"]) for event in first if event.get("seq") is not None)
            process.kill()
            process.wait(timeout=10)

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            resumed = _stream_events(client, run_id, timeout=45, after=cursor)
            sequences = [int(event["seq"]) for event in resumed]
            assert sequences == sorted(set(sequences))
            assert sum(event["event_type"] == "run.cleanup_required" for event in resumed) == 1
            assert not any(
                event["event_type"] in {"run.completed", "run.failed"} for event in resumed
            )
            cleanup = next(
                event for event in resumed if event["event_type"] == "run.cleanup_required"
            )
            assert cleanup["payload"]["category"] == "execution_lease_expired"
            assert cleanup["payload"]["retryable"] is False

            diagnostics = client.get(f"/v1/runs/{run_id}/diagnostics")
            assert diagnostics.status_code == 200, diagnostics.text
            assert diagnostics.json()["run"]["status"] == "cleanup_required"
            assert diagnostics.json()["tool_receipts"] == []

            snapshot = client.get(f"/v1/threads/{thread_id}")
            assert snapshot.status_code == 200, snapshot.text
            projected = next(item for item in snapshot.json()["runs"] if item["id"] == run_id)
            assert projected["status"] == "cleanup_required"


def test_completed_tool_receipt_is_not_replayed_when_final_model_call_is_killed(
    tmp_path: Path,
) -> None:
    port = _free_port()
    token = "process-post-tool-kill-token"
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace-post-tool-kill"
    workspace.mkdir()
    side_effects = workspace / "side-effects.log"
    headers = {"Authorization": f"Bearer {token}"}
    goal = "\n".join(
        [
            "[[e2e:post-tool-slow]]",
            _encoded_tool_goal(
                "write_file",
                {"file_path": "/side-effects.log", "content": "commit\n"},
            ),
        ]
    )

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir) as process:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            authorized = client.post(
                "/v1/workspaces",
                json={"path": str(workspace), "label": "post Tool kill point"},
            )
            assert authorized.status_code == 200, authorized.text
            accepted = client.post(
                "/v1/runs",
                json={
                    "command_id": "cmd_process_post_tool_kill_run",
                    "client_message_id": "msg_process_post_tool_kill_run",
                    "protocol_version": 1,
                    "required_capabilities": [
                        "agent.run",
                        "agent.stream",
                        "hitl",
                        "subagents",
                        "workspace.files",
                    ],
                    "goal": goal,
                    "workspace_path": str(workspace),
                    "model": "local:test:model",
                    "settings": {"memory": "off", "skills": "off", "mcp": "off"},
                },
            )
            assert accepted.status_code == 200, accepted.text
            run_id = str(accepted.json()["id"])
            first = _stream_events(client, run_id)
            permission = next(
                event["payload"] for event in first if event["event_type"] == "permission.required"
            )
            resolved = client.post(
                "/v1/commands",
                json={
                    "type": "permission.resolve",
                    "command_id": "cmd_process_post_tool_kill_approve",
                    "permission_id": permission["request_id"],
                    "decision": "approve",
                    "scope": "once",
                },
            )
            assert resolved.status_code == 200, resolved.text

            active = _stream_events(client, run_id, stop_after="llm.delta")
            assert any(event["event_type"] == "tool.completed" for event in active)
            assert any(event["event_type"] == "llm.delta" for event in active)
            assert side_effects.read_text(encoding="utf-8").splitlines() == ["commit"]
            process.kill()
            process.wait(timeout=10)

    with _runtime_process(tmp_path, port=port, token=token, data_dir=data_dir):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
            resumed = _stream_events(client, run_id, timeout=45)
            assert sum(event["event_type"] == "tool.completed" for event in resumed) == 1
            assert sum(event["event_type"] == "run.cleanup_required" for event in resumed) == 1
            assert not any(
                event["event_type"] in {"run.completed", "run.failed"} for event in resumed
            )
            assert side_effects.read_text(encoding="utf-8").splitlines() == ["commit"]

            diagnostics = client.get(f"/v1/runs/{run_id}/diagnostics")
            assert diagnostics.status_code == 200, diagnostics.text
            assert diagnostics.json()["run"]["status"] == "cleanup_required"
            receipts = [
                receipt
                for receipt in diagnostics.json()["tool_receipts"]
                if receipt["tool_name"] == "write_file"
            ]
            assert len(receipts) == 1
            assert receipts[0]["status"] == "completed"
            assert receipts[0]["attempt_count"] == 1


@contextmanager
def _runtime_process(
    tmp_path: Path,
    *,
    port: int,
    token: str,
    data_dir: Path,
) -> Iterator[subprocess.Popen[bytes]]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    log_path = tmp_path / "runtime.log"
    env = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "SHEJANE_FAKE_LLM": "1",
        # Plan-first has dedicated Runtime tests. These process tests isolate
        # restart, lease, Tool receipt, and checkpoint recovery behavior.
        "SHEJANE_PLAN_FIRST": "off",
        "SHEJANE_E2E_SLOW_SECONDS": "30",
        "SHEJANE_RUNTIME_UNKNOWN_MODEL_MAX_INPUT_TOKENS": "100000",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
    }
    sandbox_command = os.environ.get("SHEJANE_MANAGED_WORKER_SANDBOX_COMMAND")
    if sandbox_command:
        env["SHEJANE_MANAGED_WORKER_SANDBOX_COMMAND"] = sandbox_command
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "shejane_runtime",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--token",
                token,
                "--data-dir",
                str(data_dir),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_ready(port, process, log_path)
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def _wait_until_ready(port: int, process: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + 15
    url = f"http://127.0.0.1:{port}/v1/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"Runtime exited with {process.returncode}:\n{log_path.read_text(errors='replace')}"
            )
        try:
            if httpx.get(url, timeout=0.2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"Runtime did not become ready:\n{log_path.read_text(errors='replace')}")


def _stream_events(
    client: httpx.Client,
    run_id: str,
    *,
    timeout: float = 20,
    after: int | None = None,
    stop_after: str | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    params = {"after": after} if after is not None else None
    with client.stream(
        "GET",
        f"/v1/runs/{run_id}/stream",
        params=params,
        timeout=timeout,
    ) as response:
        assert response.status_code == 200, response.read().decode(errors="replace")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line.removeprefix("data:").strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            if isinstance(event, dict) and "event_type" in event:
                events.append(event)
                if event["event_type"] == stop_after:
                    break
    return events


def _encoded_tool_goal(name: str, args: dict[str, Any]) -> str:
    payload = json.dumps({"name": name, "args": args}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"[[e2e:tool:{encoded}]]"


def _wait_for_process_marker(
    marker: str,
    process: subprocess.Popen[bytes],
) -> list[int]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"Runtime exited before shell marker appeared: {process.returncode}"
            )
        result = subprocess.run(
            ["pgrep", "-f", marker],
            check=False,
            capture_output=True,
            text=True,
        )
        pids = [int(value) for value in result.stdout.split() if value.isdigit()]
        if pids:
            return pids
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for shell marker {marker}")


def _describe_process(pid: int) -> str:
    """Identify a process well enough to triage which layer of the sandbox it is.

    The sandbox wraps the command in several processes that all carry the
    marker in their argv, and a `Z` state distinguishes "never reaped" from
    "still running" -- both of which the bare pid hides.
    """

    result = subprocess.run(
        ["ps", "-o", "pid=,ppid=,pgid=,stat=,args=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    described = result.stdout.strip()
    return described or f"pid {pid} answered kill(0) but is absent from ps"


def _wait_for_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"Tool child process did not exit: {_describe_process(pid)}")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
