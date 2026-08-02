from __future__ import annotations

import asyncio
import json
import shutil
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from shejane_runtime.plugins.package import InvalidPluginPackage
from shejane_runtime.plugins.platforms import current_managed_worker_execution_platform
from shejane_runtime.plugins.runtime_assets import (
    _MAX_ASSET_ARCHIVE_BYTES,
    RuntimeAssetResolver,
    RuntimeAssetStore,
    _prepare_asset_executables,
    canonical_runtime_asset_digest,
    load_runtime_asset_manifest,
)


def _asset_tree(root: Path, *, platform: str | None = None) -> Path:
    target = platform or current_managed_worker_execution_platform() or "linux/arm64"
    manifest = {
        "schema_version": 1,
        "id": "org.libreoffice.runtime",
        "version": "25.8.7",
        "platform": target,
        "license": "MPL-2.0",
        "source_url": "https://www.libreoffice.org/download/download-libreoffice/",
        "payload": "payload/libreoffice",
        "sbom": ".shejane-runtime-asset/sbom.spdx.json",
    }
    metadata = root / ".shejane-runtime-asset"
    metadata.mkdir(parents=True)
    (metadata / "asset.json").write_text(json.dumps(manifest), encoding="utf-8")
    (metadata / "sbom.spdx.json").write_text(
        json.dumps({"spdxVersion": "SPDX-2.3"}),
        encoding="utf-8",
    )
    payload = root / "payload" / "libreoffice"
    payload.mkdir(parents=True)
    (payload / "program.bin").write_bytes(b"pinned engine")
    return root


def _pack(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def _append_link(archive_path: Path, name: str, target: str) -> None:
    with zipfile.ZipFile(archive_path, "a", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, target)


def _append_directory(archive_path: Path, name: str) -> None:
    with zipfile.ZipFile(archive_path, "a", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(name.rstrip("/") + "/")
        info.create_system = 3
        info.external_attr = (stat.S_IFDIR | 0o700) << 16
        archive.writestr(info, b"")


def test_runtime_asset_archive_limit_is_separate_from_plugin_packages() -> None:
    assert _MAX_ASSET_ARCHIVE_BYTES == 768 * 1024 * 1024


def test_runtime_asset_executable_uses_windows_chmod_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _asset_tree(tmp_path / "source")
    manifest_path = source / ".shejane-runtime-asset" / "asset.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["executables"] = ["payload/libreoffice/program.bin"]
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    manifest = load_runtime_asset_manifest(source)
    calls: list[bool] = []

    def windows_chmod(_path: Path, _mode: int, *, follow_symlinks: bool = True) -> None:
        calls.append(follow_symlinks)
        if not follow_symlinks:
            raise NotImplementedError

    monkeypatch.setattr("shejane_runtime.plugins.runtime_assets.os.chmod", windows_chmod)

    _prepare_asset_executables(source, manifest)

    assert calls == [False, True]


def test_runtime_asset_store_installs_and_resolves_exact_digest(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    store = RuntimeAssetStore(tmp_path / "data")

    first = store.install(archive)
    replay = store.install(archive)
    resolved = store.resolve(
        asset_id="org.libreoffice.runtime",
        version="25.8.7",
        platform=first.platform,
        digest=first.digest,
    )

    assert first == replay
    assert resolved == first
    assert first.digest == canonical_runtime_asset_digest(first.root)
    assert first.payload == first.root / "payload" / "libreoffice"
    assert (first.payload / "program.bin").read_bytes() == b"pinned engine"


@pytest.mark.asyncio
async def test_runtime_asset_resolver_downloads_exact_asset_once(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    digest = canonical_runtime_asset_digest(source)
    manifest = load_runtime_asset_manifest(source)
    downloads: list[str] = []

    async def download(
        url: str,
        destination: Path,
        _report_progress: Callable[[int, int | None], None],
    ) -> None:
        downloads.append(url)
        shutil.copyfile(archive, destination)

    resolver = RuntimeAssetResolver(
        tmp_path / "data",
        sources={manifest.id: "https://downloads.example.test/libreoffice.shejane-runtime-asset"},
        downloader=download,
    )

    first, replay = await asyncio.gather(
        resolver.ensure(
            asset_id=manifest.id,
            version=manifest.version,
            platform=manifest.platform,
            digest=digest,
        ),
        resolver.ensure(
            asset_id=manifest.id,
            version=manifest.version,
            platform=manifest.platform,
            digest=digest,
        ),
    )

    assert first == replay
    assert downloads == ["https://downloads.example.test/libreoffice.shejane-runtime-asset"]


@pytest.mark.asyncio
async def test_runtime_asset_resolver_reports_download_progress(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    digest = canonical_runtime_asset_digest(source)
    manifest = load_runtime_asset_manifest(source)
    halfway = asyncio.Event()
    continue_download = asyncio.Event()

    async def download(
        _url: str,
        destination: Path,
        report_progress: Callable[[int, int | None], None],
    ) -> None:
        payload = archive.read_bytes()
        midpoint = len(payload) // 2
        destination.write_bytes(payload[:midpoint])
        report_progress(midpoint, len(payload))
        halfway.set()
        await continue_download.wait()
        with destination.open("ab") as output:
            output.write(payload[midpoint:])
        report_progress(len(payload), len(payload))

    resolver = RuntimeAssetResolver(
        tmp_path / "data",
        sources={manifest.id: "https://downloads.example.test/libreoffice.shejane-runtime-asset"},
        downloader=download,
    )
    task = asyncio.create_task(
        resolver.ensure(
            asset_id=manifest.id,
            version=manifest.version,
            platform=manifest.platform,
            digest=digest,
        )
    )

    await asyncio.wait_for(halfway.wait(), timeout=1)
    assert resolver.download_progress(digest).percent == 50
    continue_download.set()
    await task
    assert resolver.download_progress(digest) is None


@pytest.mark.asyncio
async def test_runtime_asset_resolver_rejects_download_with_wrong_digest(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    manifest = load_runtime_asset_manifest(source)

    async def download(
        _url: str,
        destination: Path,
        _report_progress: Callable[[int, int | None], None],
    ) -> None:
        shutil.copyfile(archive, destination)

    resolver = RuntimeAssetResolver(
        tmp_path / "data",
        sources={manifest.id: "https://downloads.example.test/libreoffice.shejane-runtime-asset"},
        downloader=download,
    )

    with pytest.raises(InvalidPluginPackage, match="expected digest"):
        await resolver.ensure(
            asset_id=manifest.id,
            version=manifest.version,
            platform=manifest.platform,
            digest="sha256:" + "f" * 64,
        )

    packages = tmp_path / "data" / "plugins" / "runtime-assets" / "packages"
    assert not packages.exists() or list(packages.iterdir()) == []


@pytest.mark.asyncio
async def test_runtime_asset_resolver_retries_after_offline_failure(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    digest = canonical_runtime_asset_digest(source)
    manifest = load_runtime_asset_manifest(source)
    attempts = 0

    async def download(
        _url: str,
        destination: Path,
        _report_progress: Callable[[int, int | None], None],
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("offline")
        shutil.copyfile(archive, destination)

    resolver = RuntimeAssetResolver(
        tmp_path / "data",
        sources={manifest.id: "https://downloads.example.test/libreoffice.shejane-runtime-asset"},
        downloader=download,
    )

    with pytest.raises(InvalidPluginPackage, match="download failed"):
        await resolver.ensure(
            asset_id=manifest.id,
            version=manifest.version,
            platform=manifest.platform,
            digest=digest,
        )

    installed = await resolver.ensure(
        asset_id=manifest.id,
        version=manifest.version,
        platform=manifest.platform,
        digest=digest,
    )
    assert installed.digest == digest
    assert attempts == 2


def test_runtime_asset_store_rejects_wrong_platform(tmp_path: Path) -> None:
    current = current_managed_worker_execution_platform()
    wrong = "windows/amd64" if current != "windows/amd64" else "darwin/arm64"
    source = _asset_tree(tmp_path / "source", platform=wrong)
    archive = tmp_path / "wrong.shejane-runtime-asset"
    _pack(source, archive)

    with pytest.raises(InvalidPluginPackage, match="does not target this platform"):
        RuntimeAssetStore(tmp_path / "data").install(archive)


def test_runtime_asset_store_preserves_only_internal_relative_links(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    _append_link(archive, "payload/libreoffice/program-link", "program.bin")
    _append_directory(archive, "payload/libreoffice/empty-profile")

    installed = RuntimeAssetStore(tmp_path / "data").install(archive)

    link = installed.payload / "program-link"
    assert link.is_symlink()
    assert link.resolve(strict=True) == installed.payload / "program.bin"
    assert (installed.payload / "empty-profile").is_dir()


def test_runtime_asset_store_rejects_escaping_links(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    _append_link(archive, "payload/libreoffice/escape", "../../../outside")

    with pytest.raises(InvalidPluginPackage, match="link escapes"):
        RuntimeAssetStore(tmp_path / "data").install(archive)


def test_runtime_asset_store_detects_tampering_before_lease(tmp_path: Path) -> None:
    source = _asset_tree(tmp_path / "source")
    archive = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack(source, archive)
    store = RuntimeAssetStore(tmp_path / "data")
    installed = store.install(archive)
    (installed.payload / "program.bin").write_bytes(b"tampered")

    with pytest.raises(InvalidPluginPackage, match="digest changed"):
        store.resolve(
            asset_id=installed.asset_id,
            version=installed.version,
            platform=installed.platform,
            digest=installed.digest,
        )
    with pytest.raises(InvalidPluginPackage, match="digest changed"):
        store.install(archive)


def test_runtime_asset_archive_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.shejane-runtime-asset"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(".shejane-runtime-asset/asset.json", "{}")
        package.writestr("../escape", "bad")

    with pytest.raises(InvalidPluginPackage):
        RuntimeAssetStore(tmp_path / "data").install(archive)
    assert not (tmp_path / "escape").exists()
