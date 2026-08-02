"""P6 immutable plugin package and contribution snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .browser_qa import (
    BROWSER_QA_PLUGIN_ID,
    RUNTIME_ALIAS_DIGEST_CHARS,
    is_allowed_browser_qa_package,
)
from .computer_use import is_allowed_computer_use_package
from .identity import plugin_action_catalog_hash
from .manifest import load_plugin_manifest
from .ocr import is_allowed_ocr_package
from .package import InvalidPluginPackage, canonical_package_digest
from .platforms import current_managed_worker_execution_platform, current_managed_worker_platform
from .runtime_assets import RuntimeAssetDownloader, RuntimeAssetHandle, RuntimeAssetResolver


class PluginCatalogError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        if retryable is not None:
            self.retryable = retryable


class _RuntimeAssetRequired(RuntimeError):
    def __init__(
        self,
        *,
        plugin_id: str,
        asset_id: str,
        version: str,
        platform: str,
        digest: str,
    ) -> None:
        super().__init__(f"plugin {plugin_id} runtime asset is unavailable")
        self.plugin_id = plugin_id
        self.asset_id = asset_id
        self.version = version
        self.platform = platform
        self.digest = digest


@dataclass(frozen=True, slots=True)
class PluginPackageHandle:
    plugin_id: str
    version: str
    digest: str
    root: Path
    entrypoint: Path
    execution_kind: str
    runtime_assets: tuple[RuntimeAssetHandle, ...]


@dataclass(frozen=True, slots=True)
class PluginActionDescriptor:
    plugin_id: str
    plugin_version: str
    plugin_digest: str
    action_id: str
    tool_name: str
    title: str
    description: str
    action_schema_digest: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    effects: tuple[str, ...]
    determinism: str
    capabilities: tuple[str, ...]
    limits: Mapping[str, Any]
    package_root: Path
    entrypoint: Path
    entrypoint_digest: str
    execution_kind: str
    execution_handler: str | None
    runtime_assets: tuple[RuntimeAssetHandle, ...]
    model_binding: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PluginSkillDescriptor:
    plugin_id: str
    skill_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class PluginCommandDescriptor:
    plugin_id: str
    command_id: str
    title: str
    description: str
    instructions: Path
    required_actions: tuple[str, ...]


class PluginExecutionLease:
    """One run's exact package bytes and fixed contribution view."""

    def __init__(
        self,
        *,
        packages: tuple[PluginPackageHandle, ...],
        actions: tuple[PluginActionDescriptor, ...],
        skills: tuple[PluginSkillDescriptor, ...],
        commands: tuple[PluginCommandDescriptor, ...],
        runtime_assets: tuple[RuntimeAssetHandle, ...],
        action_catalog_hash: str,
        execution_context: object,
    ) -> None:
        self.packages = packages
        self.actions = actions
        self.skills = skills
        self.commands = commands
        self.runtime_assets = runtime_assets
        self.action_catalog_hash = action_catalog_hash
        self.execution_context = execution_context
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class PluginCatalog:
    def __init__(
        self,
        data_dir: Path,
        *,
        runtime_asset_sources: Mapping[str, Path | str] | None = None,
        runtime_asset_downloader: RuntimeAssetDownloader | None = None,
    ) -> None:
        self._packages_root = data_dir / "plugins" / "packages"
        self._browser_runtime_root = data_dir / "browser-qa" / "runtime"
        self._runtime_assets = RuntimeAssetResolver(
            data_dir,
            sources=runtime_asset_sources,
            downloader=runtime_asset_downloader,
        )
        self._runtime_asset_lease_lock = threading.Lock()
        self._active_runtime_asset_leases: dict[str, int] = {}

    @asynccontextmanager
    async def acquire_snapshot(
        self,
        frozen_bindings: list[dict[str, Any]],
        execution_context: object,
    ) -> AsyncIterator[PluginExecutionLease]:
        bindings = tuple(dict(binding) for binding in frozen_bindings)
        attempted: set[str] = set()
        while True:
            load_task = asyncio.create_task(
                asyncio.to_thread(
                    self._load_leased_snapshot,
                    bindings,
                    execution_context,
                )
            )
            try:
                lease = await asyncio.shield(load_task)
                break
            except asyncio.CancelledError:
                while not load_task.done():
                    try:
                        await asyncio.shield(load_task)
                    except asyncio.CancelledError:
                        continue
                if not load_task.cancelled():
                    try:
                        cancelled_lease = load_task.result()
                    except Exception:
                        pass
                    else:
                        self._release_runtime_assets(cancelled_lease)
                raise
            except _RuntimeAssetRequired as missing:
                if missing.digest in attempted:
                    raise PluginCatalogError(
                        "plugin_runtime_asset_unavailable",
                        str(missing),
                    ) from missing
                attempted.add(missing.digest)
                try:
                    await self._runtime_assets.ensure(
                        asset_id=missing.asset_id,
                        version=missing.version,
                        platform=missing.platform,
                        digest=missing.digest,
                    )
                except InvalidPluginPackage as exc:
                    raise PluginCatalogError(
                        "plugin_runtime_asset_unavailable",
                        str(missing),
                        retryable=True,
                    ) from exc
        try:
            yield lease
        finally:
            release_task = asyncio.create_task(
                asyncio.to_thread(self._release_runtime_assets, lease)
            )
            try:
                await lease.aclose()
            finally:
                release_cancelled: asyncio.CancelledError | None = None
                while not release_task.done():
                    try:
                        await asyncio.shield(release_task)
                    except asyncio.CancelledError as exc:
                        release_cancelled = exc
                release_task.result()
                if release_cancelled is not None:
                    raise release_cancelled

    async def remove_runtime_asset(self, *, asset_id: str, digest: str) -> None:
        await asyncio.to_thread(self._remove_runtime_asset, asset_id, digest)

    def _load_leased_snapshot(
        self,
        frozen_bindings: tuple[dict[str, Any], ...],
        execution_context: object,
    ) -> PluginExecutionLease:
        with self._runtime_asset_lease_lock:
            lease = self._load_snapshot(frozen_bindings, execution_context)
            for asset in lease.runtime_assets:
                self._active_runtime_asset_leases[asset.digest] = (
                    self._active_runtime_asset_leases.get(asset.digest, 0) + 1
                )
            return lease

    def _release_runtime_assets(self, lease: PluginExecutionLease) -> None:
        with self._runtime_asset_lease_lock:
            for asset in lease.runtime_assets:
                remaining = self._active_runtime_asset_leases[asset.digest] - 1
                if remaining:
                    self._active_runtime_asset_leases[asset.digest] = remaining
                else:
                    del self._active_runtime_asset_leases[asset.digest]

    def _remove_runtime_asset(self, asset_id: str, digest: str) -> None:
        with self._runtime_asset_lease_lock:
            if self._active_runtime_asset_leases.get(digest, 0):
                raise PluginCatalogError(
                    "plugin_runtime_asset_in_use",
                    "runtime asset is in use",
                )
            key = digest.removeprefix("sha256:")
            browser_alias = self._browser_runtime_root / key[:RUNTIME_ALIAS_DIGEST_CHARS]
            try:
                if asset_id == f"{BROWSER_QA_PLUGIN_ID}.runtime":
                    if browser_alias.is_symlink() or browser_alias.is_file():
                        browser_alias.unlink()
                    else:
                        shutil.rmtree(browser_alias)
                self._runtime_assets.remove(digest)
            except FileNotFoundError:
                self._runtime_assets.remove(digest)
            except (InvalidPluginPackage, OSError) as exc:
                raise PluginCatalogError(
                    "plugin_runtime_asset_delete_failed",
                    "runtime asset could not be deleted",
                ) from exc

    def _load_snapshot(
        self,
        frozen_bindings: tuple[dict[str, Any], ...],
        execution_context: object,
    ) -> PluginExecutionLease:
        packages: list[PluginPackageHandle] = []
        actions: list[PluginActionDescriptor] = []
        skills: list[PluginSkillDescriptor] = []
        commands: list[PluginCommandDescriptor] = []
        runtime_assets_by_digest: dict[str, RuntimeAssetHandle] = {}
        catalog_bindings: list[dict[str, str | None]] = []

        for binding in sorted(frozen_bindings, key=lambda item: str(item["plugin_id"])):
            digest = str(binding["digest"])
            package_root = self._packages_root / digest.removeprefix("sha256:")
            if not package_root.is_dir():
                raise PluginCatalogError(
                    "plugin_version_unavailable",
                    f"plugin {binding['plugin_id']} exact package is unavailable",
                )
            try:
                actual_digest = canonical_package_digest(package_root)
                manifest_model = load_plugin_manifest(package_root)
            except InvalidPluginPackage as exc:
                raise PluginCatalogError(
                    "plugin_version_unavailable",
                    f"plugin {binding['plugin_id']} exact package is invalid",
                ) from exc
            if actual_digest != digest:
                raise PluginCatalogError(
                    "plugin_version_unavailable",
                    f"plugin {binding['plugin_id']} exact package digest changed",
                )
            manifest = manifest_model.model_dump(mode="json")
            if manifest["id"] != binding["plugin_id"] or manifest["version"] != binding["version"]:
                raise PluginCatalogError(
                    "plugin_version_unavailable",
                    "plugin package identity does not match the frozen binding",
                )
            binding_catalog_hash = plugin_action_catalog_hash(
                manifest,
                plugin_digest=digest,
            )
            if binding_catalog_hash != binding["action_catalog_hash"]:
                raise PluginCatalogError(
                    "plugin_definition_mismatch",
                    f"plugin {binding['plugin_id']} action catalog changed",
                )

            execution = manifest["runtime"]["execution"]
            raw_model_binding = binding.get("model_binding")
            model_binding = (
                _freeze_mapping(raw_model_binding) if isinstance(raw_model_binding, dict) else None
            )
            package_runtime_assets: tuple[RuntimeAssetHandle, ...] = ()
            execution_platform: str | None = None
            if execution["kind"] == "managed_worker":
                execution_platform = current_managed_worker_execution_platform()
                if execution_platform is None or execution["platforms"] != [execution_platform]:
                    raise PluginCatalogError(
                        "plugin_platform_incompatible",
                        f"plugin {binding['plugin_id']} does not target this platform",
                    )
            elif execution["kind"] == "builtin":
                identity = {
                    "plugin_id": str(binding["plugin_id"]),
                    "version": str(binding["version"]),
                    "handler": str(execution["handler"]),
                }
                if not (
                    is_allowed_computer_use_package(**identity)
                    or is_allowed_browser_qa_package(**identity)
                    or is_allowed_ocr_package(**identity)
                ):
                    raise PluginCatalogError(
                        "plugin_version_unavailable",
                        f"plugin {binding['plugin_id']} is not an allowlisted built-in package",
                    )
                execution_platform = current_managed_worker_platform()
                if execution_platform is None or execution["platforms"] != [execution_platform]:
                    raise PluginCatalogError(
                        "plugin_platform_incompatible",
                        f"plugin {binding['plugin_id']} does not target this host platform",
                    )
            if execution_platform is not None:
                resolved_assets: list[RuntimeAssetHandle] = []
                for reference in execution.get("runtime_assets", []):
                    try:
                        asset = self._runtime_assets.resolve(
                            asset_id=str(reference["id"]),
                            version=str(reference["version"]),
                            platform=execution_platform,
                            digest=str(reference["digest"]),
                        )
                    except InvalidPluginPackage as exc:
                        raise _RuntimeAssetRequired(
                            plugin_id=str(binding["plugin_id"]),
                            asset_id=str(reference["id"]),
                            version=str(reference["version"]),
                            platform=execution_platform,
                            digest=str(reference["digest"]),
                        ) from exc
                    runtime_assets_by_digest.setdefault(asset.digest, asset)
                    resolved_assets.append(asset)
                package_runtime_assets = tuple(resolved_assets)
            entrypoint = package_root / execution.get("entrypoint", ".shejane-plugin/plugin.json")
            packages.append(
                PluginPackageHandle(
                    plugin_id=str(binding["plugin_id"]),
                    version=str(binding["version"]),
                    digest=digest,
                    root=package_root,
                    entrypoint=entrypoint,
                    execution_kind=str(execution["kind"]),
                    runtime_assets=package_runtime_assets,
                )
            )
            for action in manifest["contributions"]["actions"]:
                input_schema = _read_json_object(package_root / action["input_schema"])
                output_schema = _read_json_object(package_root / action["output_schema"])
                actions.append(
                    PluginActionDescriptor(
                        plugin_id=str(binding["plugin_id"]),
                        plugin_version=str(binding["version"]),
                        plugin_digest=digest,
                        action_id=str(action["id"]),
                        tool_name=f"plugin.{binding['plugin_id']}.{action['id']}",
                        title=str(action["title"]),
                        description=str(action["description"]),
                        action_schema_digest=_json_schema_digest(
                            input_schema=input_schema,
                            output_schema=output_schema,
                        ),
                        input_schema=_freeze_mapping(input_schema),
                        output_schema=_freeze_mapping(output_schema),
                        consumes=tuple(action["consumes"]),
                        produces=tuple(action["produces"]),
                        effects=tuple(action["effects"]),
                        determinism=str(action["determinism"]),
                        capabilities=tuple(action["capabilities"]),
                        limits=_freeze_mapping(action["limits"]),
                        package_root=package_root,
                        entrypoint=entrypoint,
                        entrypoint_digest=_file_digest(entrypoint),
                        execution_kind=str(execution["kind"]),
                        execution_handler=(
                            str(execution["handler"]) if execution["kind"] == "builtin" else None
                        ),
                        runtime_assets=package_runtime_assets,
                        model_binding=model_binding,
                    )
                )
            skills.extend(
                PluginSkillDescriptor(
                    plugin_id=str(binding["plugin_id"]),
                    skill_id=str(skill["id"]),
                    path=package_root / skill["path"],
                )
                for skill in manifest["contributions"].get("skills", [])
            )
            commands.extend(
                PluginCommandDescriptor(
                    plugin_id=str(binding["plugin_id"]),
                    command_id=str(command["id"]),
                    title=str(command["title"]),
                    description=str(command["description"]),
                    instructions=package_root / command["instructions"],
                    required_actions=tuple(command["required_actions"]),
                )
                for command in manifest["contributions"].get("commands", [])
            )
            catalog_bindings.append(
                {
                    "plugin_id": str(binding["plugin_id"]),
                    "digest": digest,
                    "action_catalog_hash": binding_catalog_hash,
                    "command_id": binding.get("command_id"),
                    "model_binding_digest": (
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                raw_model_binding,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        if isinstance(raw_model_binding, dict)
                        else None
                    ),
                }
            )

        canonical = json.dumps(
            {"protocol": "plugin-catalog-v1", "bindings": catalog_bindings},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        aggregate_hash = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        return PluginExecutionLease(
            packages=tuple(packages),
            actions=tuple(actions),
            skills=tuple(skills),
            commands=tuple(commands),
            runtime_assets=tuple(runtime_assets_by_digest.values()),
            action_catalog_hash=aggregate_hash,
            execution_context=execution_context,
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidPluginPackage("plugin action schema is invalid") from exc
    if not isinstance(value, dict):
        raise InvalidPluginPackage("plugin action schema must be an object")
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _freeze_mapping(value: dict[str, Any]) -> Mapping[str, Any]:
    return _freeze_json(value)


def _json_schema_digest(*, input_schema: dict[str, Any], output_schema: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"input": input_schema, "output": output_schema},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
