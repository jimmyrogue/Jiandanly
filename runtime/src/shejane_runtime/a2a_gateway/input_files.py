from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from a2a.types.a2a_pb2 import Message, Part

from shejane_runtime.tools.web import _pinned_transport

_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_BYTES = 200 * 1024 * 1024
_MAX_REDIRECTS = 3
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")
SUPPORTED_INPUT_MEDIA_TYPES = (
    "text/plain",
    "application/json",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/tiff",
    "image/bmp",
)


class InboundFileError(ValueError):
    pass


class InboundFileUnsupportedMediaError(InboundFileError):
    pass


class InboundFileTemporaryError(RuntimeError):
    pass


def has_file_parts(message: Message) -> bool:
    return any(part.WhichOneof("content") in {"raw", "url"} for part in message.parts)


def validate_file_part(part: Part, *, index: int) -> tuple[str, str]:
    content = part.WhichOneof("content")
    if content not in {"raw", "url"}:
        raise InboundFileError("attachment part must contain raw bytes or a URL")
    media_type = part.media_type.strip().lower()
    if not media_type or len(media_type) > 255 or _MEDIA_TYPE_RE.fullmatch(media_type) is None:
        raise InboundFileError("attachment mediaType is invalid")
    if media_type not in SUPPORTED_INPUT_MEDIA_TYPES:
        raise InboundFileUnsupportedMediaError(
            f"attachment mediaType is not supported: {media_type}"
        )
    filename = part.filename.strip()
    if not filename and content == "url":
        filename = unquote(Path(urlsplit(part.url).path).name)
    if not filename:
        filename = f"attachment-{index + 1}.bin"
    if (
        filename in {".", ".."}
        or len(filename.encode("utf-8")) > 255
        or Path(filename).name != filename
        or "\\" in filename
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in filename)
    ):
        raise InboundFileError("attachment filename is invalid")
    if content == "url":
        _validate_download_url(part.url)
    elif len(part.raw) > _MAX_FILE_BYTES:
        raise InboundFileError("attachment exceeds the 20 MiB limit")
    return filename, media_type


def _validate_download_url(value: str) -> None:
    if not value or len(value) > 2048 or any(ord(character) < 0x20 for character in value):
        raise InboundFileError("attachment URL is invalid")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise InboundFileError("attachment URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise InboundFileError("attachment URL must use HTTPS without credentials or a fragment")


class InboundFileStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    async def materialize(self, *, peer_id: str, message: Message) -> list[str]:
        files = [
            (index, part)
            for index, part in enumerate(message.parts)
            if part.WhichOneof("content") in {"raw", "url"}
        ]
        if len(files) > 10:
            raise InboundFileError("a message may contain at most 10 attachments")
        paths: list[str] = []
        total = 0
        for index, part in files:
            filename, media_type = validate_file_part(part, index=index)
            identity = hashlib.sha256(
                f"{peer_id}\0{message.message_id}\0{index}".encode()
            ).hexdigest()
            destination = self.root / identity[:2] / identity / filename
            if destination.is_symlink():
                raise InboundFileTemporaryError("attachment storage is invalid")
            if not destination.is_file():
                if part.WhichOneof("content") == "raw":
                    self._persist(destination, bytes(part.raw))
                else:
                    await self._download(part.url, destination, media_type=media_type)
            try:
                size = destination.stat().st_size
            except OSError as exc:
                raise InboundFileTemporaryError("attachment storage is unavailable") from exc
            if size <= 0:
                raise InboundFileError("attachment must not be empty")
            if size > _MAX_FILE_BYTES:
                raise InboundFileError("attachment exceeds the 20 MiB limit")
            total += size
            if total > _MAX_TOTAL_BYTES:
                raise InboundFileError("attachments exceed the 200 MiB message limit")
            paths.append(str(destination))
        return paths

    async def _download(self, url: str, destination: Path, *, media_type: str) -> None:
        current = url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            _validate_download_url(current)
            transport, reason = _pinned_transport(current, allow_fake_ip=False)
            if transport is None:
                raise InboundFileError(f"attachment URL was blocked: {reason}")
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30, connect=5),
                    follow_redirects=False,
                    transport=transport,
                ) as client:
                    async with client.stream("GET", current) as response:
                        location = response.headers.get("location")
                        if response.status_code in {301, 302, 303, 307, 308} and location:
                            if redirect_count >= _MAX_REDIRECTS:
                                raise InboundFileError("attachment URL has too many redirects")
                            current = urljoin(current, location)
                            continue
                        if response.status_code in {408, 425, 429} or response.status_code >= 500:
                            raise InboundFileTemporaryError(
                                f"attachment download returned HTTP {response.status_code}"
                            )
                        if not response.is_success:
                            raise InboundFileError(
                                f"attachment download returned HTTP {response.status_code}"
                            )
                        declared = response.headers.get("content-length")
                        if declared is not None:
                            try:
                                if int(declared) > _MAX_FILE_BYTES:
                                    raise InboundFileError("attachment exceeds the 20 MiB limit")
                            except ValueError:
                                raise InboundFileError(
                                    "attachment Content-Length is invalid"
                                ) from None
                        response_type = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if (
                            response_type
                            and response_type != "application/octet-stream"
                            and media_type != "application/octet-stream"
                            and response_type != media_type
                        ):
                            raise InboundFileError(
                                "attachment response Content-Type does not match mediaType"
                            )
                        await self._persist_stream(destination, response)
                        return
            except InboundFileError:
                raise
            except InboundFileTemporaryError:
                raise
            except httpx.HTTPError as exc:
                raise InboundFileTemporaryError("attachment download failed") from exc
        raise InboundFileError("attachment URL has too many redirects")

    async def _persist_stream(self, destination: Path, response: httpx.Response) -> None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(dir=destination.parent)
        temporary = Path(temporary_name)
        written = 0
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                    written += len(chunk)
                    if written > _MAX_FILE_BYTES:
                        raise InboundFileError("attachment exceeds the 20 MiB limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if written == 0:
                raise InboundFileError("attachment must not be empty")
            self._link_first_writer(temporary, destination)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _persist(self, destination: Path, content: bytes) -> None:
        if not content:
            raise InboundFileError("attachment must not be empty")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            self._link_first_writer(temporary, destination)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _link_first_writer(temporary: Path, destination: Path) -> None:
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
        try:
            destination.chmod(0o400)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise InboundFileTemporaryError("attachment storage is unavailable") from exc
