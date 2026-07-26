"""Runtime-owned filesystem capability wrappers."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

import yaml
from deepagents.backends import FilesystemBackend, LocalShellBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    ExecuteResponse,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from langgraph.runtime import get_runtime
from markitdown import MarkItDown

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


class RuntimeBackend(SandboxBackendProtocol):
    """Delegate filesystem calls to the backend bound to this graph invocation."""

    @staticmethod
    def _backend() -> BackendProtocol:
        context = getattr(get_runtime(), "context", None)
        backend = getattr(context, "backend", None)
        if not isinstance(backend, BackendProtocol):
            raise RuntimeError("agent workspace backend is not bound")
        return backend

    def ls(self, path: str) -> LsResult:
        return self._backend().ls(path)

    async def als(self, path: str) -> LsResult:
        return await self._backend().als(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._backend().read(file_path, offset, limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await self._backend().aread(file_path, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        return self._backend().grep(pattern, path, glob)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        return await self._backend().agrep(pattern, path, glob)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._backend().glob(pattern, path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await self._backend().aglob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._backend().write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await self._backend().awrite(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self._backend().edit(file_path, old_string, new_string, replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return await self._backend().aedit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._backend().upload_files(files)

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        return await self._backend().aupload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._backend().download_files(paths)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await self._backend().adownload_files(paths)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._backend().execute(command, timeout=timeout)  # type: ignore[attr-defined]

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,  # noqa: ASYNC109 - required by Deep Agents protocol
    ) -> ExecuteResponse:
        return await self._backend().aexecute(command, timeout=timeout)  # type: ignore[attr-defined]


def _normalize_skill_frontmatter(content: bytes | None) -> bytes | None:
    if not content:
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return content
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return content
    if not isinstance(frontmatter, dict) or not isinstance(frontmatter.get("allowed-tools"), list):
        return content
    frontmatter["allowed-tools"] = " ".join(
        tool.strip()
        for tool in frontmatter["allowed-tools"]
        if isinstance(tool, str) and tool.strip()
    )
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{header}\n---\n{text[match.end() :]}".encode()


class ReadOnlyBackend:
    """Delegate reads while rejecting every mutating backend operation."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def write(self, _file_path: str, _content: str) -> WriteResult:
        return WriteResult(error="read-only source: writes are not allowed")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    def edit(
        self,
        _file_path: str,
        _old_string: str,
        _new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        del replace_all
        return EditResult(error="read-only source: edits are not allowed")

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        return self.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [
            FileDownloadResponse(
                path=response.path,
                content=_normalize_skill_frontmatter(response.content),
                error=response.error,
            )
            for response in self._delegate.download_files(paths)
        ]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = await self._delegate.adownload_files(paths)
        return [
            FileDownloadResponse(
                path=response.path,
                content=_normalize_skill_frontmatter(response.content),
                error=response.error,
            )
            for response in responses
        ]


class ReadOnlyFileBackend(ReadOnlyBackend):
    """Expose one source file at a CompositeBackend route."""

    def __init__(
        self,
        delegate: Any,
        file_name: str,
        *,
        display_name: str | None = None,
    ) -> None:
        super().__init__(delegate)
        self._file_name = file_name
        self._display_name = display_name or file_name
        self._converted_text: str | None = None

    def _source_key(self, requested: str) -> str:
        return f"/{self._file_name}" if requested == "/" else "/__not_available__"

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if file_path == "/" and Path(self._display_name).suffix.lower() in {
            ".docx",
            ".pdf",
            ".pptx",
            ".xlsx",
        }:
            return self._read_document(offset=offset, limit=limit)
        return self._delegate.read(self._source_key(file_path), offset=offset, limit=limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if file_path == "/" and Path(self._display_name).suffix.lower() in {
            ".docx",
            ".pdf",
            ".pptx",
            ".xlsx",
        }:
            return await asyncio.to_thread(self._read_document, offset=offset, limit=limit)
        return await self._delegate.aread(
            self._source_key(file_path),
            offset=offset,
            limit=limit,
        )

    def _read_document(self, *, offset: int, limit: int) -> ReadResult:
        if self._converted_text is None:
            response = self._delegate.download_files([f"/{self._file_name}"])[0]
            if response.error:
                return ReadResult(error=response.error)
            if response.content is None:
                return ReadResult(error="Attachment has no readable content")
            extension = Path(self._display_name).suffix.lower()
            try:
                converted = MarkItDown().convert_stream(
                    io.BytesIO(response.content),
                    file_extension=extension,
                )
            except Exception as exc:  # MarkItDown wraps parser failures by format.
                return ReadResult(
                    error=f"Error reading {extension} attachment: {type(exc).__name__}"
                )
            self._converted_text = converted.text_content

        if not self._converted_text.strip():
            return ReadResult(error="Attachment contains no extractable text")
        lines = self._converted_text.splitlines(keepends=True)
        if offset >= len(lines):
            return ReadResult(
                error=f"Line offset {offset} exceeds file length ({len(lines)} lines)"
            )
        end = min(offset + limit, len(lines))
        return ReadResult(file_data=FileData(content="".join(lines[offset:end]), encoding="utf-8"))

    def ls_info(self, _path: str) -> list[FileInfo]:
        return []

    async def als_info(self, path: str) -> list[FileInfo]:
        return self.ls_info(path)

    def ls(self, _path: str) -> LsResult:
        return LsResult(entries=[])

    async def als(self, path: str) -> LsResult:
        return self.ls(path)

    def grep(
        self,
        _pattern: str,
        _path: str | None = None,
        _glob: str | None = None,
    ) -> GrepResult:
        return GrepResult(matches=[])

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        return self.grep(pattern, path, glob)

    def grep_raw(
        self,
        _pattern: str,
        _path: str | None = None,
        _glob: str | None = None,
    ) -> list[GrepMatch]:
        return []

    async def agrep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch]:
        return self.grep_raw(pattern, path, glob)

    def glob_info(self, _pattern: str, _path: str = "/") -> list[FileInfo]:
        return []

    async def aglob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return self.glob_info(pattern, path)

    def glob(self, _pattern: str, _path: str | None = None) -> GlobResult:
        return GlobResult(matches=[])

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self.glob(pattern, path)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if path != "/":
                responses.append(FileDownloadResponse(path=path, error="permission_denied"))
                continue
            result = self._delegate.download_files([f"/{self._file_name}"])[0]
            responses.append(
                FileDownloadResponse(path=path, content=result.content, error=result.error)
            )
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self.download_files(paths)
