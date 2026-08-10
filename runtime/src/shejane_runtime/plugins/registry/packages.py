"""Plugin package ingestion and installation lifecycle."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from ...store.sqlite import LocalStore, PluginStateError, PluginVersionConflictError
from ..browser_qa import BROWSER_QA_PLUGIN_ID, is_allowed_browser_qa_package
from ..computer_use import COMPUTER_USE_PLUGIN_ID, is_allowed_computer_use_package
from ..manifest import load_plugin_manifest
from ..ocr import OCR_PLUGIN_ID, is_allowed_ocr_package
from ..package import (
    SIGNATURE_PATH,
    InvalidPluginPackage,
    canonical_package_digest,
    extract_plugin_archive,
)
from ..platforms import prepare_managed_worker_entrypoint
from ..policy import PluginTrustError, verify_trusted_package
from ..runtime_assets import RuntimeAssetStore
from ..sandbox_runtime import managed_worker_release_gate
from .types import PluginRegistryError, PreparedPluginPackage


class PluginPackageRegistry:
    def __init__(
        self,
        *,
        store: LocalStore,
        data_dir: Path,
        runtime_version: str,
        runtime_assets: RuntimeAssetStore,
        current_host_platform: Callable[[], str | None],
        current_execution_platform: Callable[[], str | None],
    ) -> None:
        self._store = store
        self._root = data_dir / "plugins"
        self._runtime_version = runtime_version
        self._runtime_assets = runtime_assets
        self._current_host_platform = current_host_platform
        self._current_execution_platform = current_execution_platform

    async def install(
        self,
        *,
        principal_id: str,
        command_id: str,
        source_path: str,
        expected_digest: str | None,
        allow_unsigned: bool,
    ) -> dict[str, Any]:
        command_payload: dict[str, Any] = {
            "type": "plugin.install",
            "source_path": source_path,
            "allow_unsigned": allow_unsigned,
        }
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        replay = await self._store.accepted_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.install",
            payload=command_payload,
        )
        if replay is not None:
            return replay

        try:
            prepared = await asyncio.to_thread(
                self.ingest_package,
                source_path,
                allow_unsigned,
                expected_digest,
            )
        except InvalidPluginPackage as exc:
            raise PluginRegistryError("invalid_plugin_package", str(exc)) from exc
        try:
            receipt, _created = await self._store.install_plugin_command(
                principal_id=principal_id,
                command_id=command_id,
                command_payload=command_payload,
                manifest=prepared.manifest,
                digest=prepared.digest,
                signature_status=prepared.signature_status,
                signer_key_id=prepared.signer_key_id,
                compatibility=prepared.compatibility,
                source="local_file",
            )
        except PluginVersionConflictError as exc:
            await self.discard_new_blob(prepared)
            raise PluginRegistryError("plugin_version_conflict", str(exc), status_code=409) from exc
        return receipt

    def ingest_package(
        self,
        source_path: str,
        allow_unsigned: bool,
        expected_package_digest: str | None,
        allow_builtin: bool = False,
    ) -> PreparedPluginPackage:
        source = Path(source_path).expanduser()
        if source.suffix != ".shejane-plugin":
            raise PluginRegistryError(
                "plugin_archive_required", "plugin source must be a .shejane-plugin ZIP"
            )
        staging_root = self._root / "staging"
        packages_root = self._root / "packages"
        staging_root.mkdir(parents=True, exist_ok=True)
        packages_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="install-", dir=staging_root))
        package_root = staging / "package"
        try:
            extract_plugin_archive(source, package_root)
            manifest = load_plugin_manifest(package_root)
            if (
                manifest.id in {COMPUTER_USE_PLUGIN_ID, BROWSER_QA_PLUGIN_ID, OCR_PLUGIN_ID}
                and not allow_builtin
            ):
                raise PluginRegistryError(
                    "builtin_capability_managed",
                    "This fixed capability is managed by the SheJane Runtime",
                    status_code=409,
                )
            digest = canonical_package_digest(package_root)
            if expected_package_digest is not None and expected_package_digest != digest:
                raise PluginRegistryError(
                    "plugin_digest_mismatch",
                    "plugin package does not match expected_digest",
                    status_code=409,
                )
            if (package_root / SIGNATURE_PATH).exists():
                try:
                    signer_key_id = verify_trusted_package(
                        package_root,
                        manifest.publisher.id,
                        self._root / "trusted-publishers.json",
                    )
                except PluginTrustError as exc:
                    raise PluginRegistryError(exc.code, str(exc), status_code=409) from exc
                signature_status = "verified"
            elif not allow_unsigned:
                raise PluginRegistryError(
                    "unsigned_plugin_confirmation_required",
                    "installing this unsigned plugin requires explicit confirmation",
                    status_code=409,
                )
            else:
                signature_status = "unsigned"
                signer_key_id = None
            if manifest.runtime.execution.kind == "managed_worker":
                host_platform = self._current_host_platform()
                current_platform = self._current_execution_platform()
                target_platform = manifest.runtime.execution.platforms[0]
                if (
                    host_platform is None
                    or current_platform is None
                    or target_platform != current_platform
                ):
                    raise PluginRegistryError(
                        "plugin_platform_incompatible",
                        "Managed Worker package does not target this operating system and architecture",
                        status_code=409,
                    )
                for reference in manifest.runtime.execution.runtime_assets:
                    try:
                        self._runtime_assets.resolve(
                            asset_id=reference.id,
                            version=reference.version,
                            platform=target_platform,
                            digest=reference.digest,
                        )
                    except InvalidPluginPackage as exc:
                        raise PluginRegistryError(
                            "plugin_runtime_asset_unavailable",
                            f"required runtime asset {reference.id} is unavailable",
                            status_code=409,
                        ) from exc
                prepare_managed_worker_entrypoint(
                    package_root,
                    manifest.runtime.execution.entrypoint,
                )
                release_gate = managed_worker_release_gate(host_platform)
                if not release_gate.enabled:
                    raise PluginRegistryError(
                        "managed_worker_sandbox_unavailable",
                        "Managed Worker plugins require a production operating-system sandbox",
                        status_code=409,
                    )
            elif manifest.runtime.execution.kind == "builtin":
                identity = {
                    "plugin_id": manifest.id,
                    "version": manifest.version,
                    "handler": manifest.runtime.execution.handler,
                }
                if not (
                    is_allowed_computer_use_package(**identity)
                    or is_allowed_browser_qa_package(**identity)
                    or is_allowed_ocr_package(**identity)
                ):
                    raise PluginRegistryError(
                        "builtin_plugin_not_allowed",
                        "Built-in plugins must be supplied by this SheJane Runtime",
                        status_code=409,
                    )
                if manifest.runtime.execution.platforms != [self._current_host_platform()]:
                    raise PluginRegistryError(
                        "plugin_platform_incompatible",
                        "Built-in plugin does not target this operating system and architecture",
                        status_code=409,
                    )
            try:
                compatible = Version(self._runtime_version) >= Version(manifest.runtime.min_version)
            except InvalidVersion as exc:
                raise PluginRegistryError(
                    "plugin_runtime_version_invalid", "plugin runtime version is invalid"
                ) from exc
            destination = packages_root / digest.removeprefix("sha256:")
            created_blob = False
            if not destination.exists():
                try:
                    os.replace(package_root, destination)
                    created_blob = True
                except OSError:
                    if not destination.exists():
                        raise
            return PreparedPluginPackage(
                manifest=manifest.model_dump(mode="json"),
                digest=digest,
                compatibility="compatible" if compatible else "incompatible",
                signature_status=signature_status,
                signer_key_id=signer_key_id,
                destination=destination,
                created_blob=created_blob,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def discard_new_blob(self, package: PreparedPluginPackage) -> None:
        if package.created_blob:
            await asyncio.to_thread(shutil.rmtree, package.destination, True)

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
        self._reject_builtin_mutation(plugin_id)
        command_payload: dict[str, Any] = {
            "type": "plugin.update",
            "plugin_id": plugin_id,
            "source_path": source_path,
            "allow_unsigned": allow_unsigned,
        }
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        replay = await self._store.accepted_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.update",
            payload=command_payload,
        )
        if replay is not None:
            return replay
        try:
            prepared = await asyncio.to_thread(
                self.ingest_package,
                source_path,
                allow_unsigned,
                None,
            )
        except InvalidPluginPackage as exc:
            raise PluginRegistryError("invalid_plugin_package", str(exc)) from exc
        try:
            receipt, _created = await self._store.update_plugin_command(
                principal_id=principal_id,
                command_id=command_id,
                command_payload=command_payload,
                plugin_id=plugin_id,
                manifest=prepared.manifest,
                digest=prepared.digest,
                signature_status=prepared.signature_status,
                signer_key_id=prepared.signer_key_id,
                compatibility=prepared.compatibility,
                source="local_file",
            )
            return receipt
        except (PluginStateError, PluginVersionConflictError) as exc:
            await self.discard_new_blob(prepared)
            code = exc.code if isinstance(exc, PluginStateError) else "plugin_version_conflict"
            status_code = 404 if code == "plugin_not_found" else 409
            raise PluginRegistryError(code, str(exc), status_code=status_code) from exc

    async def rollback(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        target_digest: str,
        expected_digest: str | None,
    ) -> dict[str, Any]:
        self._reject_builtin_mutation(plugin_id)
        try:
            receipt, _created = await self._store.rollback_plugin_command(
                principal_id=principal_id,
                command_id=command_id,
                plugin_id=plugin_id,
                target_digest=target_digest,
                expected_digest=expected_digest,
            )
            return receipt
        except PluginStateError as exc:
            status_code = 404 if exc.code == "plugin_not_found" else 409
            raise PluginRegistryError(exc.code, str(exc), status_code=status_code) from exc

    async def remove(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        expected_digest: str | None,
    ) -> dict[str, Any]:
        self._reject_builtin_mutation(plugin_id)
        try:
            receipt, _created = await self._store.remove_plugin_command(
                principal_id=principal_id,
                command_id=command_id,
                plugin_id=plugin_id,
                expected_digest=expected_digest,
            )
            return receipt
        except PluginStateError as exc:
            status_code = 404 if exc.code == "plugin_not_found" else 409
            raise PluginRegistryError(exc.code, str(exc), status_code=status_code) from exc

    @staticmethod
    def _reject_builtin_mutation(plugin_id: str) -> None:
        if plugin_id in {COMPUTER_USE_PLUGIN_ID, BROWSER_QA_PLUGIN_ID, OCR_PLUGIN_ID}:
            raise PluginRegistryError(
                "builtin_capability_managed",
                "This fixed capability is managed by the SheJane Runtime",
                status_code=409,
            )
