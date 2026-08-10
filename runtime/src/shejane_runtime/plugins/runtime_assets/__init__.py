"""Approved-source resolution and download of immutable Runtime Assets."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ...tools.web import _pinned_transport
from ..package import InvalidPluginPackage
from .store import _MAX_ASSET_ARCHIVE_BYTES as _MAX_ASSET_ARCHIVE_BYTES
from .store import ASSET_MANIFEST_PATH as ASSET_MANIFEST_PATH
from .store import RuntimeAssetHandle as RuntimeAssetHandle
from .store import RuntimeAssetManifest as RuntimeAssetManifest
from .store import RuntimeAssetStore as RuntimeAssetStore
from .store import (
    _prepare_asset_executables as _prepare_asset_executables,
)
from .store import (
    canonical_runtime_asset_digest as canonical_runtime_asset_digest,
)
from .store import (
    load_runtime_asset_manifest as load_runtime_asset_manifest,
)

_MAX_ASSET_REDIRECTS = 5
RuntimeAssetProgressCallback = Callable[[int, int | None], None]
RuntimeAssetDownloader = Callable[[str, Path, RuntimeAssetProgressCallback], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RuntimeAssetDownloadProgress:
    percent: int | None


class RuntimeAssetResolver:
    """Resolve an exact asset, fetching only from Runtime-approved sources when absent."""

    def __init__(
        self,
        data_dir: Path,
        *,
        sources: Mapping[str, Path | str] | None = None,
        downloader: RuntimeAssetDownloader | None = None,
    ) -> None:
        self._store = RuntimeAssetStore(data_dir)
        self._sources = dict(sources or {})
        self._downloader = downloader or _download_runtime_asset
        self._download_root = data_dir / "plugins" / "runtime-assets" / "downloads"
        self._locks: dict[str, asyncio.Lock] = {}
        self._download_progress: dict[str, RuntimeAssetDownloadProgress] = {}

    def resolve(
        self,
        *,
        asset_id: str,
        version: str,
        platform: str,
        digest: str,
    ) -> RuntimeAssetHandle:
        return self._store.resolve(
            asset_id=asset_id,
            version=version,
            platform=platform,
            digest=digest,
        )

    def remove(self, digest: str) -> None:
        self._store.remove(digest)

    def storage_usage(self) -> tuple[dict[str, int], int]:
        return self._store.storage_usage()

    def clear_transient(self) -> None:
        self._store.clear_transient()

    def download_progress(self, digest: str) -> RuntimeAssetDownloadProgress | None:
        return self._download_progress.get(digest)

    async def ensure(
        self,
        *,
        asset_id: str,
        version: str,
        platform: str,
        digest: str,
    ) -> RuntimeAssetHandle:
        try:
            return self.resolve(
                asset_id=asset_id,
                version=version,
                platform=platform,
                digest=digest,
            )
        except InvalidPluginPackage:
            pass

        source = self._sources.get(asset_id)
        if source is None:
            raise InvalidPluginPackage("required runtime asset has no approved source")
        lock = self._locks.setdefault(digest, asyncio.Lock())
        async with lock:
            try:
                return self.resolve(
                    asset_id=asset_id,
                    version=version,
                    platform=platform,
                    digest=digest,
                )
            except InvalidPluginPackage:
                if self._store.contains(digest):
                    await asyncio.to_thread(self._store.quarantine, digest)

            try:
                archive = await self._materialize(source, digest)
                try:
                    handle = await asyncio.to_thread(
                        self._store.install,
                        archive,
                        expected_digest=digest,
                        target_platform=platform,
                    )
                finally:
                    if isinstance(source, str):
                        with contextlib.suppress(OSError):
                            archive.unlink(missing_ok=True)
            finally:
                self._download_progress.pop(digest, None)
            if (
                handle.asset_id != asset_id
                or handle.version != version
                or handle.platform != platform
                or handle.digest != digest
            ):
                raise InvalidPluginPackage("runtime asset identity does not match its reference")
            await asyncio.to_thread(self._store.clear_quarantine, digest)
            return handle

    async def _materialize(self, source: Path | str, digest: str) -> Path:
        if isinstance(source, Path):
            if not source.is_file():
                raise InvalidPluginPackage("approved runtime asset source is unavailable")
            return source
        parsed = urlsplit(source)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise InvalidPluginPackage("approved runtime asset source URL is invalid")
        self._download_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix="asset-",
            suffix=".shejane-runtime-asset",
            dir=self._download_root,
        )
        os.close(fd)
        destination = Path(raw_path)
        self._download_progress[digest] = RuntimeAssetDownloadProgress(percent=None)

        def report_progress(written: int, total: int | None) -> None:
            percent = (
                None
                if not total
                else (100 if written >= total else min(99, round(written * 100 / total)))
            )
            self._download_progress[digest] = RuntimeAssetDownloadProgress(percent=percent)

        try:
            await self._downloader(source, destination, report_progress)
        except InvalidPluginPackage:
            destination.unlink(missing_ok=True)
            raise
        except asyncio.CancelledError:
            with contextlib.suppress(OSError):
                destination.unlink(missing_ok=True)
            raise
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise InvalidPluginPackage("runtime asset download failed") from exc
        return destination


async def _download_runtime_asset(
    url: str,
    destination: Path,
    report_progress: RuntimeAssetProgressCallback,
) -> None:
    current_url = url
    try:
        for redirect_count in range(_MAX_ASSET_REDIRECTS + 1):
            parsed = urlsplit(current_url)
            if parsed.scheme != "https":
                raise InvalidPluginPackage("runtime asset download requires HTTPS")
            transport, reason = _pinned_transport(current_url)
            if transport is None:
                raise InvalidPluginPackage(f"runtime asset download was blocked: {reason}")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=15.0),
                follow_redirects=False,
                transport=transport,
            ) as client:
                async with client.stream(
                    "GET",
                    current_url,
                    headers={
                        "Accept": "application/octet-stream",
                        "User-Agent": "SheJane-Runtime-Asset/1",
                    },
                ) as response:
                    location = response.headers.get("location")
                    if response.status_code in {301, 302, 303, 307, 308} and location:
                        if redirect_count >= _MAX_ASSET_REDIRECTS:
                            raise InvalidPluginPackage(
                                "runtime asset download has too many redirects"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    declared_bytes = int(declared) if declared is not None else None
                    if declared_bytes is not None and declared_bytes > _MAX_ASSET_ARCHIVE_BYTES:
                        raise InvalidPluginPackage("runtime asset download exceeds the size limit")
                    written = 0
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                            written += len(chunk)
                            if written > _MAX_ASSET_ARCHIVE_BYTES:
                                raise InvalidPluginPackage(
                                    "runtime asset download exceeds the size limit"
                                )
                            output.write(chunk)
                            report_progress(written, declared_bytes)
                    if written == 0:
                        raise InvalidPluginPackage("runtime asset download is empty")
                    return
        raise InvalidPluginPackage("runtime asset download has too many redirects")
    except (httpx.HTTPError, ValueError) as exc:
        raise InvalidPluginPackage("runtime asset download failed") from exc
