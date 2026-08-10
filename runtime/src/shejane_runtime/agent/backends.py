"""Runtime-owned backend delegation and read-only projections."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import yaml
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

from ..document_markdown import DOCUMENT_MARKDOWN_CHAR_CAP, document_to_markdown
from .local_backends import (
    ATTACHMENT_FILE_READ_MAX_MB as ATTACHMENT_FILE_READ_MAX_MB,
)
from .local_backends import (
    MODEL_FILE_READ_MAX_MB as MODEL_FILE_READ_MAX_MB,
)
from .local_backends import (
    PDF_FILE_READ_MAX_MB as PDF_FILE_READ_MAX_MB,
)
from .local_backends import (
    RuntimeFilesystemBackend as RuntimeFilesystemBackend,
)
from .local_backends import (
    RuntimeLocalShellBackend as RuntimeLocalShellBackend,
)
from .local_backends import (
    SandboxProcessLedger as SandboxProcessLedger,
)


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
                self._converted_text, truncated = document_to_markdown(
                    response.content,
                    extension,
                )
            except Exception as exc:
                return ReadResult(
                    error=f"Error reading {extension} attachment: {type(exc).__name__}"
                )
            if truncated:
                self._converted_text = (
                    self._converted_text[:DOCUMENT_MARKDOWN_CHAR_CAP]
                    + "\n\n…(truncated at 4,000,000 characters)"
                )

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
