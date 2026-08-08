#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLATFORMS = (
    "darwin/arm64",
    "windows/amd64",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--runtime-asset-digest", required=True)
    parser.add_argument("--version", default="0.1.5")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.runtime_asset_digest):
        parser.error("--runtime-asset-digest must be a canonical SHA-256 digest")
    if not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        args.version,
    ):
        parser.error("--version must be semantic version text")
    if args.output.suffix != ".shejane-plugin":
        parser.error("--output must end in .shejane-plugin")

    with tempfile.TemporaryDirectory(prefix="ocr-plugin-package-") as temporary:
        stage = Path(temporary)
        (stage / ".shejane-plugin").mkdir()
        shutil.copytree(ROOT / "actions", stage / "actions")
        shutil.copytree(ROOT / "commands", stage / "commands")
        manifest = (ROOT / ".shejane-plugin" / "plugin.template.json").read_text(encoding="utf-8")
        manifest = (
            manifest.replace("__PLUGIN_VERSION__", args.version)
            .replace("__PLATFORM__", args.platform)
            .replace("__RUNTIME_ASSET_DIGEST__", args.runtime_asset_digest)
        )
        if "__" in manifest:
            raise RuntimeError("plugin manifest template contains an unresolved placeholder")
        (stage / ".shejane-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
        pack(stage, args.output)


def pack(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(item for item in source.rglob("*") if item.is_file()):
                relative = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                archive.writestr(info, path.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
