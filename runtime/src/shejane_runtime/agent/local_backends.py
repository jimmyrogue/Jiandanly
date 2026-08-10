"""Runtime filesystem limits and sandboxed local Shell execution."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

from deepagents.backends import FilesystemBackend, LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, ReadResult

from ..plugins.sandbox_runtime import prepare_agent_shell_command, read_process_start
from ..processes import kill_process_tree

log = logging.getLogger(__name__)

MODEL_FILE_READ_MAX_MB = 20
PDF_FILE_READ_MAX_MB = 200
ATTACHMENT_FILE_READ_MAX_MB = 200


class _BoundedReadMixin:
    """Apply the advertised backend file-size limit to direct reads."""

    def _configure_read_limits(
        self,
        *,
        default_max_mb: int,
        pdf_max_mb: int,
    ) -> None:
        self._default_read_max_bytes = default_max_mb * 1024 * 1024
        self._pdf_read_max_bytes = pdf_max_mb * 1024 * 1024
        # The dependency performs its own generic size check after this mixin.
        # Give it the larger ceiling; this mixin enforces the type-specific one.
        self.max_file_size_bytes = max(
            self._default_read_max_bytes,
            self._pdf_read_max_bytes,
        )

    def _size_error(self, file_path: str) -> str | None:
        try:
            resolved_path = self._resolve_path(file_path)  # type: ignore[attr-defined]
            max_bytes = (
                self._pdf_read_max_bytes
                if resolved_path.suffix.lower() == ".pdf"
                else self._default_read_max_bytes
            )
            if resolved_path.exists() and resolved_path.is_file():
                size = resolved_path.stat().st_size
                if size > max_bytes:
                    return (
                        f"File '{file_path}' is too large to read "
                        f"({size} bytes; limit {max_bytes} bytes / "
                        f"{max_bytes // (1024 * 1024)} MB)"
                    )
        except (OSError, RuntimeError):
            # Preserve the backend's canonical path/not-found error shape.
            pass
        return None

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if error := self._size_error(file_path):
            return ReadResult(error=error)
        return super().read(file_path, offset, limit)  # type: ignore[misc]

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        return await asyncio.to_thread(self.read, file_path, offset, limit)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if error := self._size_error(path):
                responses.append(FileDownloadResponse(path=path, error=error))
            else:
                responses.extend(super().download_files([path]))  # type: ignore[misc]
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await asyncio.to_thread(self.download_files, paths)


class RuntimeFilesystemBackend(_BoundedReadMixin, FilesystemBackend):
    """Filesystem backend with a hard direct-read size boundary."""

    def __init__(
        self,
        *args: Any,
        max_file_size_mb: int = MODEL_FILE_READ_MAX_MB,
        pdf_max_file_size_mb: int = PDF_FILE_READ_MAX_MB,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            max_file_size_mb=max(max_file_size_mb, pdf_max_file_size_mb),
            **kwargs,
        )
        self._configure_read_limits(
            default_max_mb=max_file_size_mb,
            pdf_max_mb=pdf_max_file_size_mb,
        )


class SandboxProcessLedger(Protocol):
    """Durable record of sandbox launchers an execution attempt is running.

    The in-process cleanup paths below reap their own trees. This exists only
    for the path where no cleanup code runs at all -- the Runtime being killed
    outright -- so that a later Runtime can finish the job.
    """

    async def record(self, *, pid: int, process_started_at: str, settings_path: str) -> str: ...

    async def forget(self, record_id: str) -> None: ...


class RuntimeLocalShellBackend(_BoundedReadMixin, LocalShellBackend):
    """Run async shell commands in a process group owned by the Run.

    Deep Agents' local backend delegates async execution to a worker thread
    around ``subprocess.run``. Cancelling that coroutine cannot stop the
    thread, and a timeout only kills the immediate shell process. A command
    that spawned children could therefore outlive a canceled/expired Run.

    Runtime execution uses an async subprocess in its own process group so
    timeout and task cancellation both reap the complete command tree before
    control returns to the coordinator.
    """

    def __init__(
        self,
        *args: Any,
        max_file_size_mb: int = MODEL_FILE_READ_MAX_MB,
        pdf_max_file_size_mb: int = PDF_FILE_READ_MAX_MB,
        sandbox_launcher: tuple[str, ...] | None = None,
        sandbox_ledger: SandboxProcessLedger | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._sandbox_launcher = sandbox_launcher
        self._sandbox_ledger = sandbox_ledger
        self._configure_read_limits(
            default_max_mb=max_file_size_mb,
            pdf_max_mb=pdf_max_file_size_mb,
        )

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,  # noqa: ASYNC109 - public backend protocol
    ) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )
        if self._sandbox_launcher is None:
            return ExecuteResponse(
                output="Error: Command sandbox is unavailable; execution was blocked.",
                exit_code=1,
                truncated=False,
            )

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            raise ValueError(f"timeout must be positive, got {effective_timeout}")

        process: asyncio.subprocess.Process | None = None
        communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        sandbox_record_id: str | None = None
        try:
            executable_roots = tuple(
                Path(part)
                for part in str(self._env.get("PATH") or "").split(os.pathsep)
                if part and Path(part).is_absolute()
            )
            scratch_parent = self._env.get("TMPDIR") or None
            with tempfile.TemporaryDirectory(
                prefix="shejane-agent-shell-",
                dir=scratch_parent,
            ) as scratch_value:
                scratch_root = Path(scratch_value)
                wrapped_command = prepare_agent_shell_command(
                    launcher=self._sandbox_launcher,
                    command=command,
                    workspace_root=Path(self.cwd),
                    scratch_root=scratch_root,
                    executable_roots=executable_roots,
                )
                sandbox_env = {
                    **self._env,
                    "HOME": str(scratch_root),
                    "TMPDIR": str(scratch_root),
                    "TMP": str(scratch_root),
                    "TEMP": str(scratch_root),
                }
                platform_args: dict[str, Any]
                if os.name == "nt":
                    platform_args = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                else:
                    platform_args = {"start_new_session": True}
                process = await asyncio.create_subprocess_exec(
                    *wrapped_command,
                    cwd=str(self.cwd),
                    env=sandbox_env,
                    stdin=subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **platform_args,
                )
                sandbox_record_id = await self._record_sandbox_process(
                    process.pid,
                    scratch_root / "sandbox-settings.json",
                )
                communicate_task = asyncio.create_task(process.communicate())
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate_task),
                        timeout=effective_timeout,
                    )
                except TimeoutError:
                    await _kill_shell_process_tree(process)
                    await communicate_task
                    message = f"Error: Command timed out after {effective_timeout} seconds" + (
                        " (custom timeout). The command may be stuck or require more time."
                        if timeout is not None
                        else ". For long-running commands, re-run using the timeout parameter."
                    )
                    return ExecuteResponse(output=message, exit_code=124, truncated=False)
                return self._execute_response(stdout, stderr, process.returncode or 0)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await _kill_shell_process_tree(process)
            if communicate_task is not None and not communicate_task.done():
                await asyncio.shield(communicate_task)
            raise
        except Exception as exc:
            if process is not None and process.returncode is None:
                await _kill_shell_process_tree(process)
            return ExecuteResponse(
                output=f"Error executing command ({type(exc).__name__}): {exc}",
                exit_code=1,
                truncated=False,
            )
        finally:
            # Reached on every path that leaves this frame, which is exactly
            # the set of endings where cleanup already ran. Whatever the record
            # was protecting against did not happen.
            if sandbox_record_id is not None:
                await self._forget_sandbox_process(sandbox_record_id)

    async def _record_sandbox_process(self, pid: int, settings_path: Path) -> str | None:
        """Note the launcher durably, without letting bookkeeping break the run."""

        if self._sandbox_ledger is None:
            return None
        started_at = read_process_start(pid)
        if started_at is None:
            # Already gone, so there is nothing a later Runtime could reap.
            return None
        try:
            return await self._sandbox_ledger.record(
                pid=pid,
                process_started_at=started_at,
                settings_path=str(settings_path),
            )
        except Exception:
            log.warning("could not record sandbox process %s", pid, exc_info=True)
            return None

    async def _forget_sandbox_process(self, record_id: str) -> None:
        if self._sandbox_ledger is None:
            return
        try:
            await self._sandbox_ledger.forget(record_id)
        except Exception:
            # A leftover row is safe: the reaper verifies identity before it
            # signals anything, so a stale record is skipped rather than acted on.
            log.warning("could not clear sandbox process record %s", record_id, exc_info=True)

    def _execute_response(
        self,
        stdout: bytes,
        stderr: bytes,
        returncode: int,
    ) -> ExecuteResponse:
        output_parts: list[str] = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            output_parts.extend(f"[stderr] {line}" for line in stderr_text.strip().split("\n"))
        output = "\n".join(output_parts) if output_parts else "<no output>"
        truncated = False
        encoded = output.encode("utf-8")
        if len(encoded) > self._max_output_bytes:
            output = encoded[: self._max_output_bytes].decode("utf-8", errors="ignore")
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True
        if returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {returncode}"
        return ExecuteResponse(output=output, exit_code=returncode, truncated=truncated)


async def _kill_shell_process_tree(process: asyncio.subprocess.Process) -> None:
    await kill_process_tree(process)
