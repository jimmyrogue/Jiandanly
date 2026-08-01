#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

ASSET_VERSION = "1.61.1+chromium1228.2"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--platform",
        choices=("darwin/arm64", "windows/amd64"),
        required=True,
    )
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.suffix != ".shejane-runtime-asset":
        parser.error("--output must end in .shejane-runtime-asset")
    browser = args.browser.resolve(strict=True)
    if not browser.is_dir() or browser.name != "chromium-1228":
        parser.error("--browser must be the Playwright 1.61.1 chromium-1228 directory")
    with tempfile.TemporaryDirectory(prefix="browser-qa-runtime-asset-") as temporary:
        stage = Path(temporary)
        payload = stage / "payload"
        shutil.copytree(browser, payload / "browsers" / browser.name, symlinks=True)
        metadata = stage / ".shejane-runtime-asset"
        metadata.mkdir()
        executables = sorted(
            path.relative_to(stage).as_posix()
            for path in stage.rglob("*")
            if is_executable(path, args.platform)
        )
        if not executables or len(executables) > 256:
            raise SystemExit("Browser QA Runtime Asset executable inventory is invalid")
        manifest = {
            "schema_version": 1,
            "id": "org.shejane.browser-qa.runtime",
            "version": ASSET_VERSION,
            "platform": args.platform,
            "license": "Apache-2.0 AND BSD-3-Clause",
            "source_url": "https://github.com/microsoft/playwright",
            "payload": "payload",
            "sbom": ".shejane-runtime-asset/sbom.spdx.json",
            "executables": executables,
        }
        (metadata / "asset.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        sbom = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"shejane-browser-qa-runtime-{args.platform.replace('/', '-')}",
            "documentNamespace": (
                "https://shejane.org/spdx/runtime-assets/browser-qa/"
                f"{ASSET_VERSION}/{args.platform.replace('/', '-')}"
            ),
            "creationInfo": {
                "created": "2026-07-23T00:00:00Z",
                "creators": ["Organization: SheJane"],
            },
            "packages": [
                {
                    "name": "Playwright",
                    "SPDXID": "SPDXRef-Package-Playwright",
                    "versionInfo": "1.61.1",
                    "downloadLocation": "https://github.com/microsoft/playwright",
                    "filesAnalyzed": False,
                    "licenseConcluded": "Apache-2.0",
                    "licenseDeclared": "Apache-2.0",
                    "copyrightText": "NOASSERTION",
                },
                {
                    "name": "Chromium for Testing",
                    "SPDXID": "SPDXRef-Package-Chromium",
                    "versionInfo": "149.0.7827.55",
                    "downloadLocation": "https://googlechromelabs.github.io/chrome-for-testing/",
                    "filesAnalyzed": False,
                    "licenseConcluded": "BSD-3-Clause",
                    "licenseDeclared": "BSD-3-Clause",
                    "copyrightText": "NOASSERTION",
                },
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": package,
                }
                for package in ("SPDXRef-Package-Playwright", "SPDXRef-Package-Chromium")
            ],
        }
        (metadata / "sbom.spdx.json").write_text(
            json.dumps(sbom, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        pack_asset(stage, args.output)


def is_executable(path: Path, target_platform: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if target_platform == "windows/amd64":
        return path.suffix.lower() == ".exe"
    return bool(path.stat().st_mode & 0o111)


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
                    mode = 0o700 if path.stat().st_mode & 0o111 else 0o600
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
