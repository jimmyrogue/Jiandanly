from __future__ import annotations

import asyncio
import json
import shutil
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from shejane_runtime.plugins.catalog import PluginCatalog, PluginCatalogError
from shejane_runtime.plugins.identity import plugin_action_catalog_hash
from shejane_runtime.plugins.manifest import load_plugin_manifest
from shejane_runtime.plugins.package import canonical_package_digest
from shejane_runtime.plugins.platforms import current_managed_worker_execution_platform
from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_FIXTURE = REPO_ROOT / "runtime" / "plugins" / "fixtures" / "wasi-archive"
WORKER_FIXTURE = REPO_ROOT / "runtime" / "plugins" / "fixtures" / "worker-documents"


def _binding(data_dir: Path) -> dict[str, object]:
    digest = canonical_package_digest(ARCHIVE_FIXTURE)
    package_root = data_dir / "plugins" / "packages" / digest.removeprefix("sha256:")
    package_root.parent.mkdir(parents=True)
    shutil.copytree(ARCHIVE_FIXTURE, package_root)
    manifest = load_plugin_manifest(package_root).model_dump(mode="json")
    return {
        "run_id": "run_catalog",
        "plugin_id": manifest["id"],
        "version": manifest["version"],
        "digest": digest,
        "selection_source": "enabled",
        "required": False,
        "command_id": None,
        "action_catalog_hash": plugin_action_catalog_hash(
            manifest,
            plugin_digest=digest,
        ),
    }


@pytest.mark.asyncio
async def test_catalog_acquires_exact_immutable_action_snapshot(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    catalog = PluginCatalog(tmp_path)

    async with catalog.acquire_snapshot([binding], execution_context=object()) as lease:
        assert lease.closed is False
        assert lease.action_catalog_hash.startswith("sha256:")
        assert len(lease.packages) == 1
        assert lease.packages[0].digest == binding["digest"]
        assert lease.packages[0].root.name == str(binding["digest"]).removeprefix("sha256:")
        assert [action.tool_name for action in lease.actions] == [
            "plugin.dev.shejane.fixture.archive.archive.extract"
        ]
        assert lease.actions[0].input_schema["additionalProperties"] is False
        assert lease.actions[0].package_root == lease.packages[0].root
        assert lease.skills == ()
        assert lease.commands == ()

    assert lease.closed is True


def _worker_binding_with_asset(data_dir: Path) -> tuple[dict[str, object], str]:
    platform = current_managed_worker_execution_platform() or "linux/arm64"
    asset_source = data_dir / "asset-source"
    metadata = asset_source / ".shejane-runtime-asset"
    payload = asset_source / "payload" / "engine"
    metadata.mkdir(parents=True)
    payload.mkdir(parents=True)
    (metadata / "asset.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "org.libreoffice.runtime",
                "version": "25.8.7",
                "platform": platform,
                "license": "MPL-2.0",
                "source_url": "https://www.libreoffice.org/",
                "payload": "payload/engine",
                "sbom": ".shejane-runtime-asset/sbom.spdx.json",
            }
        )
    )
    (metadata / "sbom.spdx.json").write_text('{"spdxVersion":"SPDX-2.3"}')
    (payload / "engine.bin").write_bytes(b"engine")
    asset_archive = data_dir / "engine.shejane-runtime-asset"
    with zipfile.ZipFile(asset_archive, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(asset_source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(asset_source).as_posix())
    asset = RuntimeAssetStore(data_dir).install(asset_archive)

    worker_source = data_dir / "worker-source"
    shutil.copytree(WORKER_FIXTURE, worker_source)
    manifest_path = worker_source / ".shejane-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    execution = manifest["runtime"]["execution"]
    execution["platforms"] = [platform]
    execution["runtime_assets"] = [
        {
            "id": asset.asset_id,
            "version": asset.version,
            "digest": asset.digest,
        }
    ]
    manifest_path.write_text(json.dumps(manifest))
    digest = canonical_package_digest(worker_source)
    package_root = data_dir / "plugins" / "packages" / digest.removeprefix("sha256:")
    package_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(worker_source, package_root)
    loaded = load_plugin_manifest(package_root).model_dump(mode="json")
    return (
        {
            "run_id": "run_worker_catalog",
            "plugin_id": loaded["id"],
            "version": loaded["version"],
            "digest": digest,
            "selection_source": "explicit",
            "required": True,
            "command_id": None,
            "action_catalog_hash": plugin_action_catalog_hash(
                loaded,
                plugin_digest=digest,
            ),
        },
        asset.digest,
    )


@pytest.mark.asyncio
async def test_catalog_leases_exact_managed_worker_runtime_assets(tmp_path: Path) -> None:
    binding, asset_digest = _worker_binding_with_asset(tmp_path)

    async with PluginCatalog(tmp_path).acquire_snapshot(
        [binding],
        execution_context=object(),
    ) as lease:
        assert [asset.digest for asset in lease.runtime_assets] == [asset_digest]
        assert [asset.digest for asset in lease.packages[0].runtime_assets] == [asset_digest]
        assert [asset.digest for asset in lease.actions[0].runtime_assets] == [asset_digest]


@pytest.mark.asyncio
async def test_catalog_only_removes_runtime_asset_after_lease_closes(tmp_path: Path) -> None:
    binding, asset_digest = _worker_binding_with_asset(tmp_path)
    catalog = PluginCatalog(tmp_path)
    store = RuntimeAssetStore(tmp_path)

    async with catalog.acquire_snapshot([binding], execution_context=object()):
        with pytest.raises(PluginCatalogError) as raised:
            await catalog.remove_runtime_asset(
                asset_id="org.libreoffice.runtime",
                digest=asset_digest,
            )
        assert raised.value.code == "plugin_runtime_asset_in_use"
        assert store.contains(asset_digest)

    await catalog.remove_runtime_asset(
        asset_id="org.libreoffice.runtime",
        digest=asset_digest,
    )
    assert not store.contains(asset_digest)
    await catalog.remove_runtime_asset(
        asset_id="org.libreoffice.runtime",
        digest=asset_digest,
    )


@pytest.mark.asyncio
async def test_catalog_bulk_cleanup_refuses_leased_runtime_assets(tmp_path: Path) -> None:
    binding, asset_digest = _worker_binding_with_asset(tmp_path)
    catalog = PluginCatalog(tmp_path)
    alias = tmp_path / "browser-qa" / "runtime" / asset_digest.removeprefix("sha256:")[:32]
    alias.mkdir(parents=True)
    different_digest = asset_digest[:-1] + ("0" if asset_digest[-1] != "0" else "1")
    (alias / ".shejane-runtime-digest").write_text(different_digest, encoding="utf-8")

    async with catalog.acquire_snapshot([binding], execution_context=object()):
        with pytest.raises(PluginCatalogError) as raised:
            await catalog.cleanup_runtime_assets({asset_digest}, clear_transient=False)
        assert raised.value.code == "plugin_runtime_asset_in_use"

    await catalog.cleanup_runtime_assets({asset_digest}, clear_transient=False)
    assert not RuntimeAssetStore(tmp_path).contains(asset_digest)
    assert alias.is_dir()


@pytest.mark.asyncio
async def test_catalog_releases_runtime_assets_when_snapshot_load_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, asset_digest = _worker_binding_with_asset(tmp_path)
    catalog = PluginCatalog(tmp_path)
    load_started = threading.Event()
    allow_load = threading.Event()
    original_load = catalog._load_snapshot

    def delayed_load(*args: object, **kwargs: object) -> object:
        load_started.set()
        assert allow_load.wait(timeout=1)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(catalog, "_load_snapshot", delayed_load)

    async def acquire() -> None:
        async with catalog.acquire_snapshot([binding], execution_context=object()):
            pass

    task = asyncio.create_task(acquire())
    assert await asyncio.to_thread(load_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    allow_load.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    await catalog.remove_runtime_asset(
        asset_id="org.libreoffice.runtime",
        digest=asset_digest,
    )


@pytest.mark.asyncio
async def test_catalog_releases_runtime_assets_after_repeated_cancellation(
    tmp_path: Path,
) -> None:
    binding, asset_digest = _worker_binding_with_asset(tmp_path)
    catalog = PluginCatalog(tmp_path)
    entered = asyncio.Event()
    block_executor = threading.Event()
    executor_blocked = threading.Event()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))

    async def acquire() -> None:
        async with catalog.acquire_snapshot([binding], execution_context=object()):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(acquire())
    await entered.wait()

    def occupy_executor() -> None:
        executor_blocked.set()
        assert block_executor.wait(timeout=1)

    blocker = loop.run_in_executor(None, occupy_executor)
    assert executor_blocked.wait(timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    block_executor.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await blocker

    await catalog.remove_runtime_asset(
        asset_id="org.libreoffice.runtime",
        digest=asset_digest,
    )


@pytest.mark.asyncio
async def test_catalog_marks_runtime_asset_download_failure_retryable(tmp_path: Path) -> None:
    binding, asset_digest = _worker_binding_with_asset(tmp_path)
    shutil.rmtree(
        tmp_path / "plugins" / "runtime-assets" / "packages" / asset_digest.removeprefix("sha256:")
    )

    async def offline(
        _url: str,
        _destination: Path,
        _report_progress: object,
    ) -> None:
        raise OSError("offline")

    catalog = PluginCatalog(
        tmp_path,
        runtime_asset_sources={
            "org.libreoffice.runtime": "https://downloads.example.test/runtime-asset"
        },
        runtime_asset_downloader=offline,
    )

    with pytest.raises(PluginCatalogError) as raised:
        async with catalog.acquire_snapshot([binding], execution_context=object()):
            pass

    assert raised.value.code == "plugin_runtime_asset_unavailable"
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_catalog_missing_exact_digest_never_falls_back_to_active_version(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    package_root = (
        tmp_path / "plugins" / "packages" / str(binding["digest"]).removeprefix("sha256:")
    )
    shutil.rmtree(package_root)

    with pytest.raises(PluginCatalogError) as raised:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding],
            execution_context=object(),
        ):
            pass

    assert raised.value.code == "plugin_version_unavailable"
