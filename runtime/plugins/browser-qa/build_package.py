#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
PLAYWRIGHT_VERSION = "1.61.1"


def esbuild_command(*, platform_name: str = os.name) -> list[str]:
    executable = (REPO_ROOT / "node_modules" / "esbuild" / "bin" / "esbuild").resolve(
        strict=True
    )
    if platform_name != "nt":
        return [str(executable)]
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required to build Browser QA on Windows")
    return [node, str(executable)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--platform",
        choices=("darwin/arm64", "windows/amd64"),
        required=True,
    )
    parser.add_argument("--runtime-asset-digest", required=True)
    parser.add_argument("--playwright", type=Path, required=True)
    parser.add_argument("--playwright-core", type=Path, required=True)
    parser.add_argument("--version", default="0.1.1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("--version must be semantic version text")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.runtime_asset_digest):
        parser.error("--runtime-asset-digest must be a canonical SHA-256 digest")
    if args.output.suffix != ".shejane-plugin":
        parser.error("--output must end in .shejane-plugin")
    sources = {
        "playwright": args.playwright.resolve(strict=True),
        "playwright-core": args.playwright_core.resolve(strict=True),
    }
    for name, source in sources.items():
        package = json.loads((source / "package.json").read_text(encoding="utf-8"))
        if package.get("name") != name or package.get("version") != PLAYWRIGHT_VERSION:
            parser.error(f"{name} must be exactly {PLAYWRIGHT_VERSION}")

    with tempfile.TemporaryDirectory(prefix="browser-qa-package-") as temporary:
        stage = Path(temporary)
        (stage / ".shejane-plugin").mkdir()
        shutil.copytree(ROOT / "actions", stage / "actions")
        shutil.copytree(ROOT / "commands", stage / "commands")
        payload = stage / "payload"
        payload.mkdir()
        modules = payload / "node_modules"
        modules.mkdir()
        for name, source in sources.items():
            shutil.copytree(source, modules / name, symlinks=True)
        subprocess.run(
            [
                *esbuild_command(),
                str(ROOT / "bridge" / "bridge-server.ts"),
                "--bundle",
                "--platform=node",
                "--format=esm",
                "--target=node20",
                "--external:playwright",
                "--legal-comments=none",
                f"--outfile={payload / 'bridge-server.mjs'}",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
        manifest = (ROOT / ".shejane-plugin" / "plugin.template.json").read_text(
            encoding="utf-8"
        )
        manifest = (
            manifest.replace("__PLUGIN_VERSION__", args.version)
            .replace("__PLATFORM__", args.platform)
            .replace("__RUNTIME_ASSET_DIGEST__", args.runtime_asset_digest)
        )
        (stage / ".shejane-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
        pack(stage, args.output)


def pack(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(item for item in source.rglob("*") if item.is_file()):
                relative = path.relative_to(source).as_posix()
                mode = path.stat().st_mode
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o500 if mode & stat.S_IXUSR else 0o600) << 16
                archive.writestr(info, path.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
