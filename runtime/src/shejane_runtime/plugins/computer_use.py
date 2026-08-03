"""Runtime-owned host adapter for the first-party Computer Use plugin."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Protocol

from ..processes import kill_process_tree
from .executor import ActionExecutor

MAX_FRAME_BYTES = 24 * 1024 * 1024
COMPUTER_USE_PLUGIN_ID = "org.shejane.computer-use"
COMPUTER_USE_PLUGIN_VERSION = "0.2.2"
_CHILD_ENVIRONMENT_KEYS = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)


def is_allowed_computer_use_package(*, plugin_id: str, version: str, handler: str) -> bool:
    return (
        plugin_id == COMPUTER_USE_PLUGIN_ID
        and version == COMPUTER_USE_PLUGIN_VERSION
        and handler == "computer_use"
    )


class ComputerUseError(RuntimeError):
    pass


class _ComputerUseCaller(Protocol):
    async def call(self, action: str, arguments: dict[str, Any], *, timeout_ms: int) -> Any: ...


class ComputerUseReadiness:
    """Converge the fixed Computer Use capability without exposing TCC sequencing."""

    def __init__(self, service: _ComputerUseCaller) -> None:
        self._service = service

    @staticmethod
    def stage_after(action_id: str, current_stage: str) -> str:
        return {
            "install_helper": "idle",
            "request_screen_recording": "screen_requested",
            "open_screen_recording_settings": "screen_settings_opened",
            "request_accessibility": "accessibility_requested",
            "open_accessibility_settings": "accessibility_settings_opened",
            "recheck": current_stage,
        }.get(action_id, current_stage)

    async def inspect(self, *, stage: str, revision: int) -> dict[str, Any]:
        evidence = await self._service.call("readiness.inspect", {}, timeout_ms=120_000)
        if not evidence.get("installed"):
            return self._snapshot(revision, "action_required", "install_helper", "install_helper")
        if not evidence.get("helper_ready"):
            return self._blocked(revision, "helper_unavailable")
        if not evidence.get("helper_identity_valid"):
            return self._blocked(revision, "helper_identity_invalid")
        if not evidence.get("screen_recording"):
            awaiting = stage in {"screen_requested", "screen_settings_opened"}
            return self._snapshot(
                revision,
                "awaiting_user" if awaiting else "action_required",
                "screen_recording",
                "open_screen_recording_settings" if awaiting else "request_screen_recording",
                can_recheck=awaiting,
            )
        if not evidence.get("accessibility"):
            awaiting = stage in {"accessibility_requested", "accessibility_settings_opened"}
            return self._snapshot(
                revision,
                "awaiting_user" if awaiting else "action_required",
                "accessibility",
                "open_accessibility_settings" if awaiting else "request_accessibility",
                can_recheck=awaiting,
            )
        return self._snapshot(revision, "ready", None, None)

    async def advance(self, *, action_id: str, stage: str, revision: int) -> dict[str, Any]:
        current = await self.inspect(stage=stage, revision=revision)
        allowed = current.get("action_id")
        awaiting_stage = stage in {
            "screen_requested",
            "screen_settings_opened",
            "accessibility_requested",
            "accessibility_settings_opened",
        }
        if action_id != allowed and not (action_id == "recheck" and awaiting_stage):
            if self._action_is_already_satisfied(action_id, current):
                return {**current, "revision": revision + 1}
            raise ComputerUseError("computer-use setup action is stale")

        next_stage = self.stage_after(action_id, stage)
        if action_id == "install_helper":
            await self._service.call("readiness.install", {}, timeout_ms=120_000)
        elif action_id == "request_screen_recording":
            await self._service.call(
                "readiness.request_permission",
                {"kind": "screenRecording"},
                timeout_ms=120_000,
            )
        elif action_id == "open_screen_recording_settings":
            await self._service.call(
                "readiness.open_settings",
                {"kind": "screenRecording"},
                timeout_ms=120_000,
            )
        elif action_id == "request_accessibility":
            await self._service.call(
                "readiness.request_permission",
                {"kind": "accessibility"},
                timeout_ms=120_000,
            )
        elif action_id == "open_accessibility_settings":
            await self._service.call(
                "readiness.open_settings",
                {"kind": "accessibility"},
                timeout_ms=120_000,
            )
        elif action_id == "recheck":
            await self._service.call("readiness.recheck", {}, timeout_ms=120_000)

        return await self.inspect(stage=next_stage, revision=revision + 1)

    @staticmethod
    def _action_is_already_satisfied(action_id: str, current: dict[str, Any]) -> bool:
        step = current.get("step")
        if action_id == "install_helper":
            return step != "install_helper"
        if action_id in {
            "request_screen_recording",
            "open_screen_recording_settings",
        }:
            return step in {"accessibility", None}
        if action_id in {
            "request_accessibility",
            "open_accessibility_settings",
        }:
            return current.get("state") == "ready"
        return False

    @staticmethod
    def _snapshot(
        revision: int,
        state: str,
        step: str | None,
        action_id: str | None,
        *,
        can_recheck: bool = False,
    ) -> dict[str, Any]:
        return {
            "state": state,
            "revision": revision,
            "step": step,
            "action_id": action_id,
            "can_recheck": can_recheck,
        }

    @classmethod
    def _blocked(cls, revision: int, code: str) -> dict[str, Any]:
        return {
            **cls._snapshot(
                revision,
                "blocked",
                "install_helper",
                "install_helper",
            ),
            "code": code,
        }


class ComputerUseService:
    error_type = ComputerUseError
    service_name = "computer-use"

    def __init__(self, package_root: Path, *, workspace_root: Path) -> None:
        self._package_root = package_root.resolve(strict=True)
        self._workspace_root = workspace_root.resolve(strict=True)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._request_id = 0
        self._stderr = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ComputerUseService:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def call(self, action: str, arguments: dict[str, Any], *, timeout_ms: int) -> Any:
        async with self._lock:
            process = await self._ensure_process()
            assert process.stdin is not None and process.stdout is not None
            self._request_id += 1
            request_id = self._request_id
            frame = (
                json.dumps(
                    {"id": request_id, "action": action, "arguments": arguments},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            if len(frame) > MAX_FRAME_BYTES:
                raise self._error("request exceeds the protocol limit")
            process.stdin.write(frame)
            await process.stdin.drain()
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readline(), timeout=max(0.1, timeout_ms / 1000)
                )
            except TimeoutError:
                await self.aclose()
                raise self._error("action timed out") from None
            except ValueError:
                await self.aclose()
                raise self._error("response exceeds the protocol limit") from None
            if not raw:
                detail = self._stderr.decode(errors="replace").strip()
                await self.aclose()
                raise self.error_type(detail or f"{self.service_name} service exited unexpectedly")
            if len(raw) > MAX_FRAME_BYTES:
                await self.aclose()
                raise self._error("response exceeds the protocol limit")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                await self.aclose()
                raise self._error("service returned invalid JSON") from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                await self.aclose()
                raise self._error("response identity changed")
            if isinstance(response.get("error"), dict):
                raise self.error_type(str(response["error"].get("message") or "action failed"))
            if "result" not in response:
                raise self._error("service omitted its result")
            return response["result"]

    def _error(self, message: str) -> ComputerUseError:
        return self.error_type(f"{self.service_name} {message}")

    def _extra_environment(self) -> dict[str, str]:
        return {
            "SHEJANE_COMPUTER_USE_PACKAGE_ROOT": str(self._package_root),
            "SHEJANE_COMPUTER_USE_WORKSPACE": str(self._workspace_root),
        }

    def _bridge_path(self) -> Path:
        return self._package_root / "payload" / "bridge-server.mjs"

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        bridge = self._bridge_path()
        if not bridge.is_file():
            raise self._error("bridge is missing from the plugin package")
        configured_node = os.environ.get("SHEJANE_RUNTIME_NODE_PATH", "").strip()
        node = configured_node or shutil.which("node") or ""
        if not node or not Path(node).is_file():
            raise self._error("requires the Runtime-provided Node.js executable")
        env = {key: os.environ[key] for key in _CHILD_ENVIRONMENT_KEYS if key in os.environ}
        env.update({"ELECTRON_RUN_AS_NODE": "1", **self._extra_environment()})
        self._stderr.clear()
        self._process = await asyncio.create_subprocess_exec(
            node,
            bridge,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace_root,
            env=env,
            start_new_session=os.name != "nt",
            limit=MAX_FRAME_BYTES + 1,
        )
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._capture_stderr(self._process.stderr))
        return self._process

    async def _capture_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            remaining = 64 * 1024 - len(self._stderr)
            if remaining > 0:
                self._stderr.extend(chunk[:remaining])

    async def aclose(self) -> None:
        process, self._process = self._process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        if process is not None and process.returncode is None:
            if os.name == "nt":
                await kill_process_tree(process)
            else:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    await kill_process_tree(process)
        if stderr_task is not None:
            await asyncio.gather(stderr_task, return_exceptions=True)


class ComputerUseActionExecutor(ActionExecutor):
    def __init__(self, service: ComputerUseService, action_id: str) -> None:
        self._service = service
        self._action_id = action_id

    async def invoke(
        self,
        invocation: dict[str, Any],
        *,
        input_root: Path,
        output_root: Path,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        del input_root, output_root, on_progress
        output = await self._service.call(
            self._action_id,
            dict(invocation["arguments"]),
            timeout_ms=int(invocation["limits"]["timeout_ms"]),
        )
        return {
            "schema_version": 1,
            "invocation_id": invocation["invocation_id"],
            "operation_id": invocation["operation_id"],
            "status": "succeeded",
            "output": output,
            "artifacts": [],
        }
