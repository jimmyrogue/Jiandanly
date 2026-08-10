"""Runtime-owned plugin control-plane facade."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...store.sqlite import LocalStore
from ..browser_qa import BROWSER_QA_PLUGIN_ID
from ..catalog import PluginCatalog
from ..computer_use import COMPUTER_USE_PLUGIN_ID
from ..ocr import OCR_PLUGIN_ID
from ..platforms import (
    current_managed_worker_execution_platform,
    current_managed_worker_platform,
)
from ..runtime_assets import RuntimeAssetStore
from .fixed_capabilities import FixedCapabilityRegistry
from .installations import PluginInstallations
from .packages import PluginPackageRegistry
from .runtime_assets import PluginRuntimeAssets
from .types import PluginRegistryError as PluginRegistryError


class PluginRegistry:
    def __init__(
        self,
        *,
        store: LocalStore,
        data_dir: Path,
        runtime_version: str,
        plugin_catalog: PluginCatalog,
        computer_use_package: Path | None = None,
        browser_qa_package: Path | None = None,
        ocr_package: Path | None = None,
    ) -> None:
        runtime_assets = RuntimeAssetStore(data_dir)
        fixed_packages = {
            plugin_id: package
            for plugin_id, package in (
                (COMPUTER_USE_PLUGIN_ID, computer_use_package),
                (BROWSER_QA_PLUGIN_ID, browser_qa_package),
                (OCR_PLUGIN_ID, ocr_package),
            )
            if package is not None
        }
        self._packages = PluginPackageRegistry(
            store=store,
            data_dir=data_dir,
            runtime_version=runtime_version,
            runtime_assets=runtime_assets,
            current_host_platform=lambda: current_managed_worker_platform(),
            current_execution_platform=lambda: current_managed_worker_execution_platform(),
        )
        self._runtime_asset_registry = PluginRuntimeAssets(
            store=store,
            runtime_assets=runtime_assets,
            plugin_catalog=plugin_catalog,
            current_platform=lambda: current_managed_worker_platform(),
        )
        self._fixed_capability_registry = FixedCapabilityRegistry(
            store=store,
            data_dir=data_dir,
            fixed_packages=fixed_packages,
            ingest_package=self._packages.ingest_package,
            discard_new_blob=self._packages.discard_new_blob,
        )
        self._installations = PluginInstallations(
            store=store,
            computer_use_readiness=self.computer_use_readiness,
        )

    async def install_runtime_asset(
        self,
        *,
        principal_id: str,
        command_id: str,
        source_path: str,
        expected_digest: str | None,
    ) -> dict[str, Any]:
        return await self._runtime_asset_registry.install_runtime_asset(
            principal_id=principal_id,
            command_id=command_id,
            source_path=source_path,
            expected_digest=expected_digest,
        )

    async def fixed_runtime_asset_status(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        return await self._runtime_asset_registry.fixed_runtime_asset_status(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )

    async def prepare_fixed_runtime_asset(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        return await self._runtime_asset_registry.prepare_fixed_runtime_asset(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )

    async def remove_fixed_runtime_asset(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any]:
        return await self._runtime_asset_registry.remove_fixed_runtime_asset(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )

    async def runtime_asset_storage(self) -> dict[str, int]:
        return await self._runtime_asset_registry.runtime_asset_storage()

    async def cleanup_runtime_asset_storage(self, scope: str) -> dict[str, int]:
        return await self._runtime_asset_registry.cleanup_runtime_asset_storage(scope)

    async def install(
        self,
        *,
        principal_id: str,
        command_id: str,
        source_path: str,
        expected_digest: str | None,
        allow_unsigned: bool,
    ) -> dict[str, Any]:
        return await self._packages.install(
            principal_id=principal_id,
            command_id=command_id,
            source_path=source_path,
            expected_digest=expected_digest,
            allow_unsigned=allow_unsigned,
        )

    async def update(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        source_path: str,
        expected_digest: str | None,
        allow_unsigned: bool,
    ) -> dict[str, Any]:
        return await self._packages.update(
            principal_id=principal_id,
            command_id=command_id,
            plugin_id=plugin_id,
            source_path=source_path,
            expected_digest=expected_digest,
            allow_unsigned=allow_unsigned,
        )

    async def rollback(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        target_digest: str,
        expected_digest: str | None,
    ) -> dict[str, Any]:
        return await self._packages.rollback(
            principal_id=principal_id,
            command_id=command_id,
            plugin_id=plugin_id,
            target_digest=target_digest,
            expected_digest=expected_digest,
        )

    async def remove(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        expected_digest: str | None,
    ) -> dict[str, Any]:
        return await self._packages.remove(
            principal_id=principal_id,
            command_id=command_id,
            plugin_id=plugin_id,
            expected_digest=expected_digest,
        )

    async def list(self, *, principal_id: str) -> list[dict[str, Any]]:
        return await self._installations.list(principal_id=principal_id)

    async def inspect(self, *, principal_id: str, plugin_id: str) -> dict[str, Any]:
        return await self._installations.inspect(principal_id=principal_id, plugin_id=plugin_id)

    async def set_enabled(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        expected_digest: str | None,
        enabled: bool,
    ) -> dict[str, Any]:
        return await self._installations.set_enabled(
            principal_id=principal_id,
            command_id=command_id,
            plugin_id=plugin_id,
            expected_digest=expected_digest,
            enabled=enabled,
        )

    async def computer_use_readiness(self, *, principal_id: str) -> dict[str, Any]:
        return await self._fixed_capability_registry.computer_use_readiness(
            principal_id=principal_id
        )

    async def advance_computer_use_setup(
        self,
        *,
        principal_id: str,
        command_id: str,
        expected_revision: int,
        action_id: str,
    ) -> dict[str, Any]:
        return await self._fixed_capability_registry.advance_computer_use_setup(
            principal_id=principal_id,
            command_id=command_id,
            expected_revision=expected_revision,
            action_id=action_id,
        )

    async def initialize_fixed_capabilities(self, principal_id: str) -> None:
        await self._fixed_capability_registry.initialize_fixed_capabilities(principal_id)

    async def bind_model(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        binding_id: str,
        requested_model: str,
        model_binding: dict[str, Any],
        expected_digest: str | None,
    ) -> dict[str, Any]:
        return await self._installations.bind_model(
            principal_id=principal_id,
            command_id=command_id,
            plugin_id=plugin_id,
            binding_id=binding_id,
            requested_model=requested_model,
            model_binding=model_binding,
            expected_digest=expected_digest,
        )
