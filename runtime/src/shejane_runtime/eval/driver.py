"""Real eval driver — drives a RUNNING local runtime over HTTP and parses its
SSE stream into a Trajectory. Used by `make eval` against the live stack
(needs a Runtime with a real configured provider for a meaningful score).

The SSE wire format is the Runtime's canonical envelope (docs/runtime-protocol.md):
each `data:` line is JSON {event_type, payload, ...}; `data: [DONE]` ends it.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import httpx

from .harness import EvalCase, Trajectory


class HttpRuntimeDriver:
    def __init__(self, base_url: str, token: str, timeout: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def runtime_version(self) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/v1/health")
            response.raise_for_status()
            payload = response.json()
        return str(payload.get("version") or "unknown")

    async def run(self, case: EvalCase) -> Trajectory:
        if case.workspace_files:
            with tempfile.TemporaryDirectory(prefix=f"shejane-eval-{case.id}-") as temporary:
                workspace = Path(temporary).resolve()
                for relative, content in case.workspace_files.items():
                    target = (workspace / relative).resolve()
                    target.relative_to(workspace)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                return await self._run(case, workspace)
        return await self._run(case, Path(case.workspace_path) if case.workspace_path else None)

    async def _run(self, case: EvalCase, workspace: Path | None) -> Trajectory:
        command_suffix = uuid.uuid4().hex
        body: dict[str, object] = {
            "command_id": f"cmd_eval_{command_suffix}",
            "client_message_id": f"msg_eval_{command_suffix}",
            "protocol_version": 1,
            "required_capabilities": ["agent.run", "agent.stream"],
            "goal": case.goal,
            "model": case.model,
            "permission_mode": case.permission_mode,
        }
        if workspace:
            body["workspace_path"] = str(workspace)
        if case.settings:
            body["settings"] = case.settings
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if workspace:
                authorized = await client.post(
                    f"{self.base_url}/v1/workspaces",
                    json={"path": str(workspace), "label": f"eval:{case.id}"},
                    headers=self._headers,
                )
                authorized.raise_for_status()
            created = await client.post(
                f"{self.base_url}/v1/runs", json=body, headers=self._headers
            )
            created.raise_for_status()
            run_id = created.json()["id"]
            trajectory = await self._stream(client, run_id, case)
            if workspace:
                for relative in case.expect.files_contain:
                    target = (workspace / relative).resolve()
                    target.relative_to(workspace.resolve())
                    if target.is_file():
                        trajectory.workspace_results[relative] = target.read_text(
                            encoding="utf-8", errors="replace"
                        )
            return trajectory

    async def _stream(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        case: EvalCase,
    ) -> Trajectory:
        traj = Trajectory()
        async with client.stream(
            "GET", f"{self.base_url}/v1/runs/{run_id}/stream", headers=self._headers
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        break
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                _apply_event(traj, event)
                await self._resolve_wait(client, event, case)
        return traj

    async def _resolve_wait(
        self,
        client: httpx.AsyncClient,
        event: dict,
        case: EvalCase,
    ) -> None:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            return
        command: dict[str, object] | None = None
        if event_type == "permission.required" and case.permission_decision:
            command = {
                "type": "permission.resolve",
                "command_id": f"cmd_eval_permission_{uuid.uuid4().hex}",
                "permission_id": request_id,
                "decision": case.permission_decision,
                "scope": "once",
            }
        elif event_type == "question.asked" and case.question_answers:
            questions = payload.get("questions") or []
            answers = {
                str(question.get("id") or request_id): case.question_answers
                for question in questions
                if isinstance(question, dict)
            }
            command = {
                "type": "question.answer",
                "command_id": f"cmd_eval_question_{uuid.uuid4().hex}",
                "question_id": request_id,
                "answers": answers or {request_id: case.question_answers},
            }
        elif event_type == "plan.approval_required" and case.approve_plans:
            command = {
                "type": "plan.resolve",
                "command_id": f"cmd_eval_plan_{uuid.uuid4().hex}",
                "approval_id": request_id,
                "decision": "approve",
            }
        if command is not None:
            response = await client.post(
                f"{self.base_url}/v1/commands",
                json=command,
                headers=self._headers,
            )
            response.raise_for_status()


def _apply_event(traj: Trajectory, event: dict) -> None:
    event_type = event.get("event_type", "")
    traj.event_counts[event_type] = traj.event_counts.get(event_type, 0) + 1
    payload = event.get("payload") or {}
    if event_type == "llm.delta":
        traj.final_text += str(payload.get("content", ""))
    elif event_type in ("tool.completed", "tool.failed"):
        tool = payload.get("tool") or payload.get("name")
        if tool:
            traj.tool_calls.append(str(tool))
        traj.steps += 1
    elif event_type == "llm.usage":
        traj.input_tokens += int(payload.get("input_tokens", 0) or 0)
        traj.output_tokens += int(payload.get("output_tokens", 0) or 0)
    elif event_type == "run.completed":
        traj.terminal_status = "completed"
        final = payload.get("final_text")
        if final:
            traj.final_text = str(final)
        # run.completed carries authoritative per-turn totals.
        if payload.get("input_tokens") or payload.get("output_tokens"):
            traj.input_tokens = int(payload.get("input_tokens", 0) or 0)
            traj.output_tokens = int(payload.get("output_tokens", 0) or 0)
        traj.model_calls = int(payload.get("model_calls", 0) or 0)
    elif event_type == "run.failed":
        traj.terminal_status = "failed"
        traj.failed = True
        traj.error = str(payload.get("message", "run failed"))
