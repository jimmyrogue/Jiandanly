#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore

BASE_ASSET_VERSION = "3.9.1+ppocrv6-small.1"
ASSET_VERSION = "3.9.1+ppocrv6-small.2"
WORKER_COMPONENT_VERSION = "0.1.4"
PLATFORMS = ("darwin/arm64", "windows/amd64")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--base-asset", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.suffix != ".shejane-runtime-asset":
        parser.error("--output must end in .shejane-runtime-asset")
    worker = validate_worker(args.worker, args.platform, parser)
    worker_name = "ocr-worker.exe" if args.platform == "windows/amd64" else "ocr-worker"

    with tempfile.TemporaryDirectory(prefix="ocr-composite-runtime-asset-") as temporary:
        work = Path(temporary)
        installed = RuntimeAssetStore(work / "store").install(
            args.base_asset.resolve(strict=True),
            target_platform=args.platform,
        )
        if (
            installed.asset_id != "org.rapidocr.runtime"
            or installed.version != BASE_ASSET_VERSION
            or installed.platform != args.platform
        ):
            parser.error("--base-asset identity is incompatible")
        stage = work / "stage"
        shutil.copytree(installed.root, stage, symlinks=True)
        shutil.copytree(worker, stage / "payload" / "worker", symlinks=True)

        metadata = stage / ".shejane-runtime-asset"
        manifest_path = metadata / "asset.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = ASSET_VERSION
        manifest["license"] = "Apache-2.0 AND AGPL-3.0-only"
        manifest["executables"] = sorted(
            {*manifest["executables"], f"payload/worker/{worker_name}"}
        )
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        worker_digest = tree_digest(worker)
        update_sbom(stage, manifest, worker_digest, args.platform, parser)
        licenses = stage / "licenses"
        licenses.mkdir(exist_ok=True)
        shutil.copy2(
            Path(__file__).resolve().parents[3] / "LICENSE",
            licenses / "LICENSE.shejane-ocr-worker",
        )
        (metadata / "composite.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "base_digest": installed.digest,
                    "worker_digest": worker_digest,
                    "worker_entrypoint": f"payload/worker/{worker_name}",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        pack_asset(stage, args.output)


def validate_worker(worker: Path, platform: str, parser: argparse.ArgumentParser) -> Path:
    if worker.is_symlink():
        parser.error("--worker must be a regular onedir bundle")
    root = worker.resolve(strict=True)
    if not root.is_dir():
        parser.error("--worker must be a regular onedir bundle")
    name = "ocr-worker.exe" if platform == "windows/amd64" else "ocr-worker"
    entrypoint = root / name
    if entrypoint.is_symlink() or not entrypoint.is_file():
        parser.error("--worker entrypoint is unavailable")
    for path in root.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = path.resolve(strict=True)
                target.relative_to(root)
            except (FileNotFoundError, OSError, ValueError):
                parser.error("--worker contains an unsafe entry")
            if not (target.is_file() or target.is_dir()):
                parser.error("--worker contains an unsafe entry")
        elif not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            parser.error("--worker contains an unsafe entry")
    return root


def update_sbom(
    stage: Path,
    manifest: dict[str, object],
    worker_digest: str,
    platform_name: str,
    parser: argparse.ArgumentParser,
) -> None:
    try:
        sbom_path = stage / str(manifest["sbom"])
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        packages = sbom["packages"]
        relationships = sbom["relationships"]
        creation_info = sbom["creationInfo"]
        if not all(
            (
                isinstance(packages, list),
                isinstance(relationships, list),
                isinstance(creation_info, dict),
            )
        ):
            raise TypeError
    except (KeyError, OSError, TypeError, json.JSONDecodeError):
        parser.error("--base-asset SBOM is incompatible")
    worker_id = "SPDXRef-Package-SheJane-OCR-Worker"
    if any(isinstance(item, dict) and item.get("SPDXID") == worker_id for item in packages):
        parser.error("--base-asset SBOM already describes the OCR Worker")
    packages.append(
        {
            "name": "SheJane OCR Worker",
            "SPDXID": worker_id,
            "versionInfo": WORKER_COMPONENT_VERSION,
            "downloadLocation": "https://github.com/ColdFlame/shejane",
            "filesAnalyzed": False,
            "licenseConcluded": "AGPL-3.0-only",
            "licenseDeclared": "AGPL-3.0-only",
            "copyrightText": "NOASSERTION",
            "checksums": [
                {
                    "algorithm": "SHA256",
                    "checksumValue": worker_digest.removeprefix("sha256:"),
                }
            ],
        }
    )
    relationships.append(
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": worker_id,
        }
    )
    sbom["name"] = f"shejane-rapidocr-runtime-{platform_name.replace('/', '-')}-composite"
    sbom["documentNamespace"] = (
        "https://shejane.org/spdx/runtime-assets/rapidocr/"
        f"{ASSET_VERSION}/{platform_name.replace('/', '-')}/"
        f"{worker_digest.removeprefix('sha256:')}"
    )
    creation_info["comment"] = f"Composite OCR Worker uses CPython {platform.python_version()}"
    sbom_path.write_text(
        json.dumps(sbom, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        metadata = path.lstat()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(stat.S_IFMT(metadata.st_mode).to_bytes(4, "big"))
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def pack_asset(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(source.rglob("*")):
                relative = path.relative_to(source).as_posix()
                if path.is_symlink():
                    archive.writestr(zip_info(relative, stat.S_IFLNK | 0o777), os.readlink(path))
                elif path.is_dir():
                    archive.writestr(zip_info(relative + "/", stat.S_IFDIR | 0o700), b"")
                elif path.is_file():
                    mode = 0o500 if path.stat().st_mode & 0o111 else 0o600
                    with archive.open(zip_info(relative, stat.S_IFREG | mode), "w") as target:
                        with path.open("rb") as source_file:
                            shutil.copyfileobj(source_file, target, length=1024 * 1024)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = mode << 16
    return info


if __name__ == "__main__":
    main()
