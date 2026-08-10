"""Downloadable Runtime Asset operations for installed fixed plugins."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...store.sqlite import LocalStore
from ..browser_qa import BROWSER_QA_PLUGIN_ID
from ..catalog import PluginCatalog, PluginCatalogError
from ..identity import plugin_action_catalog_hash
from ..ocr import OCR_PLUGIN_ID
from ..package import InvalidPluginPackage
from ..runtime_assets import RuntimeAssetStore
from .types import PluginRegistryError


class PluginRuntimeAssets:
    def __init__(
        self,
        *,
        store: LocalStore,
        runtime_assets: RuntimeAssetStore,
        plugin_catalog: PluginCatalog,
        current_platform: Callable[[], str | None],
    ) -> None:
        self._store = store
        self._runtime_assets = runtime_assets
        self._plugin_catalog = plugin_catalog
        self._current_platform = current_platform

    async def install_runtime_asset(
        self,
        *,
        principal_id: str,
        command_id: str,
        source_path: str,
        expected_digest: str | None,
    ) -> dict[str, Any]:
        command_payload: dict[str, Any] = {
            "type": "plugin.runtime_asset.install",
            "source_path": source_path,
        }
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        replay = await self._store.accepted_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.runtime_asset.install",
            payload=command_payload,
        )
        if replay is not None:
            return replay
        try:
            handle = await asyncio.to_thread(
                self._runtime_assets.install,
                Path(source_path).expanduser(),
                expected_digest=expected_digest,
            )
        except InvalidPluginPackage as exc:
            raise PluginRegistryError(
                "invalid_runtime_asset",
                str(exc),
                status_code=409,
            ) from exc
        receipt = {
            "type": "plugin.runtime_asset.install",
            "command_id": command_id,
            "asset_id": handle.asset_id,
            "version": handle.version,
            "platform": handle.platform,
            "digest": handle.digest,
            "installed": True,
        }
        return await self._store.record_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.runtime_asset.install",
            payload=command_payload,
            receipt=receipt,
        )

    async def fixed_runtime_asset_status(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        record = await self._fixed_runtime_asset_record(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )
        reference = record["manifest"]["runtime"]["execution"]["runtime_assets"][0]
        platform = self._current_platform()
        downloaded = False
        if platform is not None:
            try:
                await asyncio.to_thread(
                    self._runtime_assets.resolve,
                    asset_id=str(reference["id"]),
                    version=str(reference["version"]),
                    platform=platform,
                    digest=str(reference["digest"]),
                )
            except InvalidPluginPackage:
                pass
            else:
                downloaded = True
        status: dict[str, Any] = {
            "plugin_id": plugin_id,
            "available": True,
            "downloaded": downloaded,
        }
        progress = self._plugin_catalog.runtime_asset_download_progress(str(reference["digest"]))
        if progress is not None:
            status["downloading"] = True
            status["download_progress"] = progress.percent
        return status

    async def prepare_fixed_runtime_asset(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        record = await self._fixed_runtime_asset_record(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )
        binding = {
            "plugin_id": record["plugin_id"],
            "version": record["version"],
            "digest": record["digest"],
            "required": True,
            "action_catalog_hash": plugin_action_catalog_hash(
                record["manifest"],
                plugin_digest=record["digest"],
            ),
        }
        try:
            async with self._plugin_catalog.acquire_snapshot(
                [binding],
                execution_context=object(),
            ) as lease:
                if len(lease.runtime_assets) != 1:
                    raise PluginRegistryError(
                        "fixed_runtime_asset_unavailable",
                        "fixed plugin does not declare exactly one Runtime Asset",
                        status_code=409,
                    )
        except PluginCatalogError as exc:
            raise PluginRegistryError(
                exc.code,
                str(exc),
                status_code=503 if getattr(exc, "retryable", False) else 409,
            ) from exc
        return {"plugin_id": plugin_id, "downloaded": True}

    async def remove_fixed_runtime_asset(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        record = await self._fixed_runtime_asset_record(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )
        reference = record["manifest"]["runtime"]["execution"]["runtime_assets"][0]
        try:
            await self._plugin_catalog.remove_runtime_asset(
                asset_id=str(reference["id"]),
                digest=str(reference["digest"]),
            )
        except PluginCatalogError as exc:
            raise PluginRegistryError(exc.code, str(exc), status_code=409) from exc
        return {"plugin_id": plugin_id, "downloaded": False}

    async def runtime_asset_storage(self) -> dict[str, int]:
        protected = await self._store.referenced_runtime_asset_digests()
        packages, transient_bytes = await self._plugin_catalog.runtime_asset_storage()
        return self._runtime_asset_storage_summary(packages, transient_bytes, protected)

    async def cleanup_runtime_asset_storage(self, scope: str) -> dict[str, int]:
        if scope not in {"history", "all"}:
            raise PluginRegistryError(
                "runtime_asset_cleanup_scope_invalid",
                "runtime asset cleanup scope is invalid",
                status_code=422,
            )
        protected = await self._store.referenced_runtime_asset_digests()
        before_packages, before_transient = await self._plugin_catalog.runtime_asset_storage()
        targets = None if scope == "all" else set(before_packages) - protected
        try:
            await self._plugin_catalog.cleanup_runtime_assets(
                targets,
                clear_transient=scope == "all",
            )
        except PluginCatalogError as exc:
            raise PluginRegistryError(exc.code, str(exc), status_code=409) from exc
        after_packages, after_transient = await self._plugin_catalog.runtime_asset_storage()
        result = self._runtime_asset_storage_summary(
            after_packages,
            after_transient,
            protected,
        )
        result["freed_bytes"] = max(
            0,
            sum(before_packages.values())
            + before_transient
            - sum(after_packages.values())
            - after_transient,
        )
        return result

    @staticmethod
    def _runtime_asset_storage_summary(
        packages: dict[str, int],
        transient_bytes: int,
        protected: set[str],
    ) -> dict[str, int]:
        history = {digest: size for digest, size in packages.items() if digest not in protected}
        return {
            "total_bytes": sum(packages.values()) + transient_bytes,
            "history_bytes": sum(history.values()),
            "asset_count": len(packages),
            "history_asset_count": len(history),
        }

    async def _fixed_runtime_asset_record(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        if plugin_id not in {BROWSER_QA_PLUGIN_ID, OCR_PLUGIN_ID}:
            raise PluginRegistryError(
                "fixed_runtime_asset_unavailable",
                "plugin does not have a downloadable fixed Runtime Asset",
                status_code=409,
            )
        record = await self._store.get_plugin(principal_id=principal_id, plugin_id=plugin_id)
        if record is None:
            raise PluginRegistryError(
                "plugin_not_found", "plugin is not installed", status_code=404
            )
        references = record["manifest"]["runtime"]["execution"].get("runtime_assets", [])
        if len(references) != 1:
            raise PluginRegistryError(
                "fixed_runtime_asset_unavailable",
                "fixed plugin does not declare exactly one Runtime Asset",
                status_code=409,
            )
        return record
