"""Content-addressed local storage for immutable Runtime Assets."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, ValidationError

from ..manifest import ManagedWorkerPlatform, PackagePath, PluginId, Semver
from ..package import InvalidPluginPackage, canonical_tree_digest, extract_canonical_archive
from ..platforms import current_managed_worker_execution_platform

ASSET_MANIFEST_PATH = ".shejane-runtime-asset/asset.json"
_ASSET_DIGEST_DOMAIN = b"shejane-runtime-asset-v1\0"
_MAX_ASSET_ARCHIVE_BYTES = 768 * 1024 * 1024
_MAX_ASSET_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ASSET_FILES = 50_000


class RuntimeAssetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    id: PluginId
    version: Semver
    platform: ManagedWorkerPlatform
    license: str = Field(min_length=1, max_length=100)
    source_url: AnyUrl
    payload: PackagePath
    sbom: PackagePath
    executables: list[PackagePath] = Field(default_factory=list, max_length=256)


@dataclass(frozen=True, slots=True)
class RuntimeAssetHandle:
    asset_id: str
    version: str
    platform: str
    digest: str
    root: Path
    payload: Path
    license: str
    source_url: str
    sbom: Path


def _tree_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except FileNotFoundError:
            return 0
    if not path.is_dir():
        return 0
    total = 0
    pending = [path]
    while pending:
        root = pending.pop()
        try:
            entries = list(os.scandir(root))
        except FileNotFoundError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(Path(entry.path))
            else:
                total += info.st_size
    return total


def canonical_runtime_asset_digest(root: Path) -> str:
    return canonical_tree_digest(
        root,
        domain=_ASSET_DIGEST_DOMAIN,
        required_manifest=ASSET_MANIFEST_PATH,
        excluded_paths=frozenset(),
        max_total_bytes=_MAX_ASSET_TOTAL_BYTES,
        package_label="runtime asset",
        allow_internal_symlinks=True,
        max_files=_MAX_ASSET_FILES,
    )


def load_runtime_asset_manifest(root: Path) -> RuntimeAssetManifest:
    path = root / ASSET_MANIFEST_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = RuntimeAssetManifest.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise InvalidPluginPackage("runtime asset manifest is invalid") from exc

    payload = root / manifest.payload
    sbom = root / manifest.sbom
    if payload.is_symlink() or not payload.is_dir() or sbom.is_symlink() or not sbom.is_file():
        raise InvalidPluginPackage("runtime asset manifest references are invalid")
    try:
        payload.resolve(strict=True).relative_to(root.resolve(strict=True))
        sbom.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise InvalidPluginPackage("runtime asset manifest references escape the asset") from exc

    if len(manifest.executables) != len(set(manifest.executables)):
        raise InvalidPluginPackage("runtime asset executables must be unique")
    for relative in manifest.executables:
        executable = root / relative
        if executable.is_symlink() or not executable.is_file():
            raise InvalidPluginPackage("runtime asset executable is invalid")
        try:
            executable.resolve(strict=True).relative_to(payload.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise InvalidPluginPackage(
                "runtime asset executable must be inside the payload"
            ) from exc
    return manifest


class RuntimeAssetStore:
    """Install and resolve exact runtime asset bytes without executing them."""

    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir / "plugins" / "runtime-assets"

    def contains(self, digest: str) -> bool:
        return (self._root / "packages" / digest.removeprefix("sha256:")).is_dir()

    def storage_usage(self) -> tuple[dict[str, int], int]:
        packages: dict[str, int] = {}
        packages_root = self._root / "packages"
        if packages_root.is_dir():
            for path in packages_root.iterdir():
                if (
                    path.is_dir()
                    and len(path.name) == 64
                    and all(char in "0123456789abcdef" for char in path.name)
                ):
                    packages[f"sha256:{path.name}"] = _tree_size(path)
        transient_bytes = sum(
            _tree_size(self._root / name) for name in ("downloads", "quarantine", "staging")
        )
        return packages, transient_bytes

    def clear_transient(self) -> None:
        for name in ("downloads", "quarantine", "staging"):
            shutil.rmtree(self._root / name, ignore_errors=True)

    def quarantine(self, digest: str) -> None:
        key = digest.removeprefix("sha256:")
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise InvalidPluginPackage("runtime asset digest is invalid")
        source = self._root / "packages" / key
        if not source.is_dir():
            return
        quarantine_root = self._root / "quarantine"
        quarantine_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = quarantine_root / key
        shutil.rmtree(destination, ignore_errors=True)
        try:
            os.replace(source, destination)
        except OSError as exc:
            raise InvalidPluginPackage("invalid runtime asset could not be quarantined") from exc

    def clear_quarantine(self, digest: str) -> None:
        shutil.rmtree(
            self._root / "quarantine" / digest.removeprefix("sha256:"),
            ignore_errors=True,
        )

    def remove(self, digest: str) -> None:
        key = digest.removeprefix("sha256:")
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise InvalidPluginPackage("runtime asset digest is invalid")
        for path in (self._root / "quarantine" / key, self._root / "packages" / key):
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path)
            except FileNotFoundError:
                pass

    def install(
        self,
        source: Path,
        *,
        expected_digest: str | None = None,
        target_platform: str | None = None,
    ) -> RuntimeAssetHandle:
        if source.suffix != ".shejane-runtime-asset":
            raise InvalidPluginPackage("runtime asset source must be a .shejane-runtime-asset ZIP")
        staging_root = self._root / "staging"
        packages_root = self._root / "packages"
        staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        packages_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="install-", dir=staging_root))
        asset_root = staging / "asset"
        try:
            extract_canonical_archive(
                source,
                asset_root,
                required_manifest=ASSET_MANIFEST_PATH,
                max_archive_bytes=_MAX_ASSET_ARCHIVE_BYTES,
                max_total_bytes=_MAX_ASSET_TOTAL_BYTES,
                archive_label="runtime asset",
                allow_internal_symlinks=True,
                max_files=_MAX_ASSET_FILES,
            )
            manifest = load_runtime_asset_manifest(asset_root)
            current = target_platform or current_managed_worker_execution_platform()
            if current is None or manifest.platform != current:
                raise InvalidPluginPackage("runtime asset does not target this platform")
            digest = canonical_runtime_asset_digest(asset_root)
            if expected_digest is not None and digest != expected_digest:
                raise InvalidPluginPackage("runtime asset does not match expected digest")
            _prepare_asset_executables(asset_root, manifest)
            destination = packages_root / digest.removeprefix("sha256:")
            if not destination.exists():
                try:
                    os.replace(asset_root, destination)
                except OSError:
                    if not destination.exists():
                        raise
            return self.resolve(
                asset_id=manifest.id,
                version=manifest.version,
                platform=manifest.platform,
                digest=digest,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def resolve(
        self,
        *,
        asset_id: str,
        version: str,
        platform: str,
        digest: str,
    ) -> RuntimeAssetHandle:
        root = self._root / "packages" / digest.removeprefix("sha256:")
        if not root.is_dir():
            raise InvalidPluginPackage("required runtime asset is not installed")
        actual_digest = canonical_runtime_asset_digest(root)
        if actual_digest != digest:
            raise InvalidPluginPackage("runtime asset digest changed")
        manifest = load_runtime_asset_manifest(root)
        if manifest.id != asset_id or manifest.version != version or manifest.platform != platform:
            raise InvalidPluginPackage("runtime asset identity does not match its reference")
        return self._handle(root, manifest, digest)

    @staticmethod
    def _handle(
        root: Path,
        manifest: RuntimeAssetManifest,
        digest: str,
    ) -> RuntimeAssetHandle:
        return RuntimeAssetHandle(
            asset_id=manifest.id,
            version=manifest.version,
            platform=manifest.platform,
            digest=digest,
            root=root,
            payload=root / manifest.payload,
            license=manifest.license,
            source_url=str(manifest.source_url),
            sbom=root / manifest.sbom,
        )


def _prepare_asset_executables(root: Path, manifest: RuntimeAssetManifest) -> None:
    for relative in manifest.executables:
        executable = root / relative
        try:
            mode = executable.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise OSError
            try:
                os.chmod(executable, 0o500, follow_symlinks=False)
            except NotImplementedError:
                os.chmod(executable, 0o500)
        except OSError as exc:
            raise InvalidPluginPackage("runtime asset executable cannot be prepared") from exc
