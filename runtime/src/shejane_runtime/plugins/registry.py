"""Runtime-owned plugin installation and query control plane."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from ..store.sqlite import LocalStore, PluginStateError, PluginVersionConflictError
from .browser_qa import BROWSER_QA_PLUGIN_ID, is_allowed_browser_qa_package
from .catalog import PluginCatalog, PluginCatalogError
from .computer_use import (
    COMPUTER_USE_PLUGIN_ID,
    ComputerUseError,
    ComputerUseReadiness,
    ComputerUseService,
    is_allowed_computer_use_package,
)
from .identity import plugin_action_catalog_hash
from .manifest import load_plugin_manifest
from .ocr import OCR_PLUGIN_ID, is_allowed_ocr_package
from .package import (
    SIGNATURE_PATH,
    InvalidPluginPackage,
    canonical_package_digest,
    extract_plugin_archive,
)
from .platforms import (
    current_managed_worker_execution_platform,
    current_managed_worker_platform,
    prepare_managed_worker_entrypoint,
)
from .policy import PluginTrustError, verify_trusted_package
from .runtime_assets import RuntimeAssetStore
from .sandbox_runtime import managed_worker_release_gate


class PluginRegistryError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class _PreparedPackage:
    manifest: dict[str, Any]
    digest: str
    compatibility: str
    signature_status: str
    signer_key_id: str | None
    destination: Path
    created_blob: bool


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
        self._store = store
        self._root = data_dir / "plugins"
        self._data_dir = data_dir
        self._runtime_version = runtime_version
        self._runtime_assets = RuntimeAssetStore(data_dir)
        self._plugin_catalog = plugin_catalog
        self._fixed_packages = {
            plugin_id: package
            for plugin_id, package in (
                (COMPUTER_USE_PLUGIN_ID, computer_use_package),
                (BROWSER_QA_PLUGIN_ID, browser_qa_package),
                (OCR_PLUGIN_ID, ocr_package),
            )
            if package is not None
        }
        self._fixed_capabilities_ready_for: set[str] = set()
        self._fixed_capability_lock = asyncio.Lock()

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
        platform = current_managed_worker_platform()
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
                self._ingest_package,
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
            await self._discard_new_blob(prepared)
            raise PluginRegistryError("plugin_version_conflict", str(exc), status_code=409) from exc
        return receipt

    def _ingest_package(
        self,
        source_path: str,
        allow_unsigned: bool,
        expected_package_digest: str | None,
        allow_builtin: bool = False,
    ) -> _PreparedPackage:
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
                host_platform = current_managed_worker_platform()
                current_platform = current_managed_worker_execution_platform()
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
                if manifest.runtime.execution.platforms != [current_managed_worker_platform()]:
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
            return _PreparedPackage(
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

    async def _discard_new_blob(self, package: _PreparedPackage) -> None:
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
                self._ingest_package,
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
            await self._discard_new_blob(prepared)
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

    async def list(self, *, principal_id: str) -> list[dict[str, Any]]:
        records = await self._store.list_plugins(principal_id=principal_id)
        return [_plugin_summary(record) for record in records]

    async def inspect(self, *, principal_id: str, plugin_id: str) -> dict[str, Any]:
        record = await self._store.get_plugin(principal_id=principal_id, plugin_id=plugin_id)
        if record is None:
            raise PluginRegistryError(
                "plugin_not_found", "plugin is not installed", status_code=404
            )
        manifest = record["manifest"]
        versions = await self._store.list_plugin_versions(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )
        return {
            **_plugin_summary(record),
            "description": manifest["description"],
            "license": manifest.get("license"),
            "actions": [
                {
                    key: action[key]
                    for key in (
                        "id",
                        "title",
                        "description",
                        "consumes",
                        "produces",
                        "effects",
                        "determinism",
                        "capabilities",
                        "limits",
                    )
                }
                for action in manifest["contributions"]["actions"]
            ],
            "skills": [
                {key: skill[key] for key in ("id", "path")}
                for skill in manifest["contributions"].get("skills", [])
            ],
            "commands": [
                {key: command[key] for key in ("id", "title", "description", "required_actions")}
                for command in manifest["contributions"].get("commands", [])
            ],
            "mcp_servers": [
                {key: binding[key] for key in ("id", "path")}
                for binding in manifest["contributions"].get("mcp_servers", [])
            ],
            "versions": versions,
            "model_binding": _model_binding_summary(record.get("model_binding")),
        }

    async def set_enabled(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        expected_digest: str | None,
        enabled: bool,
    ) -> dict[str, Any]:
        if plugin_id == COMPUTER_USE_PLUGIN_ID and enabled:
            readiness = await self.computer_use_readiness(principal_id=principal_id)
            if readiness["state"] != "ready":
                raise PluginRegistryError(
                    "plugin_setup_required",
                    "Finish Computer Use setup before enabling it",
                    status_code=409,
                )
        command_type = "plugin.enable" if enabled else "plugin.disable"
        try:
            receipt, _created = await self._store.set_plugin_enabled_command(
                principal_id=principal_id,
                command_id=command_id,
                command_type=command_type,
                plugin_id=plugin_id,
                expected_digest=expected_digest,
                enabled=enabled,
            )
            return receipt
        except PluginStateError as exc:
            status_code = 404 if exc.code == "plugin_not_found" else 409
            raise PluginRegistryError(exc.code, str(exc), status_code=status_code) from exc

    async def computer_use_readiness(self, *, principal_id: str) -> dict[str, Any]:
        package = await self._ensure_computer_use(principal_id)
        if package is None:
            return {
                "state": "blocked",
                "revision": 0,
                "step": None,
                "action_id": None,
                "can_recheck": False,
                "code": "unsupported_platform",
            }
        flow = await self._store.get_plugin_setup_flow(
            principal_id=principal_id, plugin_id=COMPUTER_USE_PLUGIN_ID
        )
        try:
            async with ComputerUseService(package, workspace_root=self._data_dir) as service:
                return await ComputerUseReadiness(service).inspect(
                    stage=str(flow["stage"]), revision=int(flow["revision"])
                )
        except ComputerUseError as exc:
            raise PluginRegistryError(
                "computer_use_readiness_failed", str(exc), status_code=503
            ) from exc

    async def advance_computer_use_setup(
        self,
        *,
        principal_id: str,
        command_id: str,
        expected_revision: int,
        action_id: str,
    ) -> dict[str, Any]:
        payload = {
            "type": "plugin.setup.advance",
            "plugin_id": COMPUTER_USE_PLUGIN_ID,
            "expected_revision": expected_revision,
            "action_id": action_id,
        }
        replay = await self._store.accepted_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.setup.advance",
            payload=payload,
        )
        if replay is not None:
            return replay
        package = await self._ensure_computer_use(principal_id)
        if package is None:
            raise PluginRegistryError(
                "unsupported_platform",
                "Computer Use is unavailable on this platform",
                status_code=409,
            )
        flow = await self._store.get_plugin_setup_flow(
            principal_id=principal_id, plugin_id=COMPUTER_USE_PLUGIN_ID
        )
        stage = str(flow["stage"])
        if int(flow["revision"]) != expected_revision:
            raise PluginRegistryError(
                "plugin_setup_stale", "Computer Use setup state changed", status_code=409
            )
        next_stage = ComputerUseReadiness.stage_after(action_id, stage)
        try:
            async with ComputerUseService(package, workspace_root=self._data_dir) as service:
                snapshot = await ComputerUseReadiness(service).advance(
                    action_id=action_id,
                    stage=stage,
                    revision=expected_revision,
                )
        except ComputerUseError as exc:
            raise PluginRegistryError(
                "computer_use_setup_failed", str(exc), status_code=503
            ) from exc
        try:
            advanced = await self._store.begin_plugin_setup_action(
                principal_id=principal_id,
                plugin_id=COMPUTER_USE_PLUGIN_ID,
                expected_revision=expected_revision,
                next_stage=next_stage,
            )
        except PluginStateError as exc:
            raise PluginRegistryError(exc.code, str(exc), status_code=409) from exc
        if int(snapshot["revision"]) != int(advanced["revision"]):
            raise PluginRegistryError(
                "plugin_setup_state_invalid", "Computer Use setup revision changed", status_code=500
            )
        receipt = {
            "type": "plugin.setup.advance",
            "command_id": command_id,
            "plugin_id": COMPUTER_USE_PLUGIN_ID,
            "readiness": snapshot,
        }
        return await self._store.record_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.setup.advance",
            payload=payload,
            receipt=receipt,
        )

    async def _ensure_computer_use(self, principal_id: str) -> Path | None:
        source = self._fixed_packages.get(COMPUTER_USE_PLUGIN_ID)
        if source is None or not source.is_file():
            return None
        async with self._fixed_capability_lock:
            return await self._ensure_fixed_plugin(principal_id, COMPUTER_USE_PLUGIN_ID, source)

    async def initialize_fixed_capabilities(self, principal_id: str) -> None:
        if principal_id in self._fixed_capabilities_ready_for:
            return
        async with self._fixed_capability_lock:
            if principal_id in self._fixed_capabilities_ready_for:
                return
            for plugin_id, source in self._fixed_packages.items():
                if source.is_file():
                    await self._ensure_fixed_plugin(principal_id, plugin_id, source)
            self._fixed_capabilities_ready_for.add(principal_id)

    async def _ensure_fixed_plugin(self, principal_id: str, plugin_id: str, source: Path) -> Path:
        prepared = await asyncio.to_thread(
            self._ingest_package,
            str(source),
            True,
            None,
            True,
        )
        if prepared.manifest["id"] != plugin_id:
            await self._discard_new_blob(prepared)
            raise PluginRegistryError(
                "builtin_capability_unavailable",
                "fixed capability package identity changed",
                status_code=409,
            )
        current = await self._store.get_plugin(principal_id=principal_id, plugin_id=plugin_id)
        payload = {
            "type": "runtime.builtin.ensure",
            "plugin_id": plugin_id,
            "digest": prepared.digest,
        }
        try:
            if current is None:
                await self._store.install_plugin_command(
                    principal_id=principal_id,
                    command_id=f"builtin-install:{prepared.digest}",
                    command_payload=payload,
                    manifest=prepared.manifest,
                    digest=prepared.digest,
                    signature_status="unsigned",
                    signer_key_id=None,
                    compatibility=prepared.compatibility,
                    source="runtime_builtin",
                    command_type="runtime.builtin.ensure",
                    receipt_type="runtime.builtin.ensure",
                )
            elif current["digest"] != prepared.digest:
                await self._store.update_plugin_command(
                    principal_id=principal_id,
                    command_id=f"builtin-update:{prepared.digest}",
                    command_payload=payload,
                    plugin_id=plugin_id,
                    manifest=prepared.manifest,
                    digest=prepared.digest,
                    signature_status="unsigned",
                    signer_key_id=None,
                    compatibility=prepared.compatibility,
                    source="runtime_builtin",
                    command_type="runtime.builtin.ensure",
                    receipt_type="runtime.builtin.ensure",
                )
        except (PluginStateError, PluginVersionConflictError) as exc:
            await self._discard_new_blob(prepared)
            raise PluginRegistryError(
                "builtin_capability_unavailable", str(exc), status_code=409
            ) from exc
        return prepared.destination

    @staticmethod
    def _reject_builtin_mutation(plugin_id: str) -> None:
        if plugin_id in {COMPUTER_USE_PLUGIN_ID, BROWSER_QA_PLUGIN_ID, OCR_PLUGIN_ID}:
            raise PluginRegistryError(
                "builtin_capability_managed",
                "This fixed capability is managed by the SheJane Runtime",
                status_code=409,
            )

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
        try:
            receipt, _created = await self._store.bind_plugin_model_command(
                principal_id=principal_id,
                command_id=command_id,
                plugin_id=plugin_id,
                binding_id=binding_id,
                requested_model=requested_model,
                model_binding=model_binding,
                expected_digest=expected_digest,
            )
            return receipt
        except PluginStateError as exc:
            status_code = 404 if exc.code == "plugin_not_found" else 409
            raise PluginRegistryError(exc.code, str(exc), status_code=status_code) from exc


def _plugin_summary(record: dict[str, Any]) -> dict[str, Any]:
    manifest = record["manifest"]
    return {
        "id": record["plugin_id"],
        "name": manifest["name"],
        "description": manifest["description"],
        "version": record["version"],
        "digest": record["digest"],
        "publisher": {
            "id": manifest["publisher"]["id"],
            "name": manifest["publisher"]["name"],
        },
        "execution_kind": record["execution_kind"],
        "signature_status": record["signature_status"],
        "compatibility": record["compatibility"],
        "enabled": record["enabled"],
        "retired": record["installation_retired_at"] is not None,
    }


def _model_binding_summary(binding: Any) -> dict[str, Any] | None:
    if not isinstance(binding, dict):
        return None
    return {
        "id": str(binding["id"]),
        "requested_model": str(binding["requested_model"]),
        "connection_id": str(binding["connection_id"]),
        "connection_version": int(binding["connection_version"]),
        "model_id": str(binding["model_id"]),
    }
