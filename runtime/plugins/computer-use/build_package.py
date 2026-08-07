#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from shejane_runtime.plugins.package import canonical_package_digest

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
UPSTREAM_COMMIT = "9f59ed0eeac09b115897732c46b794ee8ca4e5b0"
UPSTREAM_VERSION = "0.5.0"
PLATFORMS = ("darwin/arm64",)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--version", default="0.2.3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--helper-app-output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", args.version):
        parser.error("--version must be semantic version text")
    if args.output.suffix != ".shejane-plugin":
        parser.error("--output must end in .shejane-plugin")
    if args.helper_app_output.suffix != ".app":
        parser.error("--helper-app-output must end in .app")
    upstream = args.upstream.resolve(strict=True)
    head = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != UPSTREAM_COMMIT:
        parser.error(f"--upstream must be pinned to {UPSTREAM_COMMIT}")
    package = json.loads((upstream / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != UPSTREAM_VERSION or package.get("license") != "MIT":
        parser.error("upstream package identity changed")

    with tempfile.TemporaryDirectory(prefix="computer-use-package-") as temporary:
        stage = Path(temporary)
        (stage / ".shejane-plugin").mkdir()
        shutil.copytree(ROOT / "actions", stage / "actions")
        shutil.copytree(ROOT / "commands", stage / "commands")
        payload = stage / "payload"
        payload.mkdir()
        build_bridge(upstream, payload / "bridge-server.mjs")
        copy_upstream_runtime(upstream, payload / "upstream")
        patch_upstream_runtime(payload / "upstream")
        build_native_helper_app(payload / "upstream", args.platform, args.helper_app_output)
        manifest = (ROOT / ".shejane-plugin" / "plugin.template.json").read_text(encoding="utf-8")
        manifest = manifest.replace("__PLUGIN_VERSION__", args.version).replace(
            "__PLATFORM__", args.platform
        )
        if "__" in manifest:
            raise RuntimeError("plugin manifest contains an unresolved placeholder")
        (stage / ".shejane-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
        digest = canonical_package_digest(stage)
        pack(stage, args.output)
        print(digest)


def build_bridge(upstream: Path, output: Path) -> None:
    source = (ROOT / "bridge" / "bridge-server.ts").read_text(encoding="utf-8")
    source = source.replace("__UPSTREAM_BRIDGE__", (upstream / "src" / "bridge.ts").as_posix())
    source = source.replace(
        "__UPSTREAM_HELPER__", (upstream / "src" / "platform" / "macos" / "helper.ts").as_posix()
    )
    entry = output.parent / "bridge-entry.ts"
    entry.write_text(source, encoding="utf-8")
    try:
        subprocess.run(
            [
                "pnpm",
                "exec",
                "esbuild",
                str(entry),
                "--bundle",
                "--platform=node",
                "--format=esm",
                "--target=node20",
                "--minify",
                "--legal-comments=none",
                f"--alias:@earendil-works/pi-coding-agent={ROOT / 'bridge' / 'pi-agent-stub.ts'}",
                f"--outfile={output}",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
    finally:
        entry.unlink(missing_ok=True)


def copy_upstream_runtime(upstream: Path, destination: Path) -> None:
    paths = (
        "package.json",
        "LICENSE",
        "scripts/setup-helper.mjs",
        "src/platform/macos/helper-path.mjs",
        "native/macos/agent_cursor.swift",
        "native/macos/agent_cursor_motion.swift",
        "native/macos/bridge.swift",
    )
    for relative in paths:
        source = upstream / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def patch_upstream_runtime(upstream: Path) -> None:
    bridge = upstream / "native/macos/bridge.swift"
    source = bridge.read_text(encoding="utf-8")
    source = source.replace(
        'case "registerPermissions":\n\t\t\treturn try registerPermissions()',
        'case "registerPermissions":\n\t\t\treturn try registerPermissions(request)',
    )
    old = """\tprivate func registerPermissions() throws -> [String: Any] {
\t\tlet options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
\t\tlet accessibility = AXIsProcessTrustedWithOptions(options)
\t\tif #available(macOS 10.15, *) {
\t\t\t_ = CGRequestScreenCaptureAccess()
\t\t}
\t\tlet capturable = screenRecordingCapturable()
\t\treturn [
\t\t\t\"accessibility\": accessibility,
\t\t\t\"screenRecording\": capturable,
\t\t\t\"screenRecordingCapturable\": capturable,
\t\t]
\t}
"""
    new = """\tprivate func registerPermissions(_ request: [String: Any]) throws -> [String: Any] {
\t\tlet kind = request[\"kind\"] as? String
\t\tguard kind == nil || kind == \"accessibility\" || kind == \"screenRecording\" else {
\t\t\tthrow BridgeFailure(message: \"Unknown permission kind '\\(kind!)'\", code: \"invalid_args\")
\t\t}
\t\tlet accessibility: Bool
\t\tif kind == nil || kind == \"accessibility\" {
\t\t\tlet options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
\t\t\taccessibility = AXIsProcessTrustedWithOptions(options)
\t\t} else {
\t\t\taccessibility = AXIsProcessTrusted()
\t\t}
\t\tif kind == nil || kind == \"screenRecording\", #available(macOS 10.15, *) {
\t\t\t_ = CGRequestScreenCaptureAccess()
\t\t}
\t\tlet capturable = screenRecordingCapturable()
\t\treturn [
\t\t\t\"accessibility\": accessibility,
\t\t\t\"screenRecording\": capturable,
\t\t\t\"screenRecordingCapturable\": capturable,
\t\t]
\t}
"""
    if old not in source:
        raise RuntimeError("pinned macOS permission bridge changed")
    bridge.write_text(source.replace(old, new), encoding="utf-8")

    setup = upstream / "scripts/setup-helper.mjs"
    setup_source = setup.read_text(encoding="utf-8")
    old_helper_path = """function prebuiltAppPathForArch(arch) {
	return path.join(rootDir, "prebuilt", "macos", arch, "pi-computer-use.app");
}
"""
    new_helper_path = """function prebuiltAppPathForArch(arch) {
	return process.env.SHEJANE_COMPUTER_USE_HELPER_APP || path.join(rootDir, "prebuilt", "macos", arch, "pi-computer-use.app");
}
"""
    if old_helper_path not in setup_source:
        raise RuntimeError("pinned macOS helper path changed")
    setup_source = setup_source.replace(old_helper_path, new_helper_path)
    marker = "<key>LSMinimumSystemVersion</key><string>14.0</string>"
    usage = (
        marker
        + "\\n<key>NSScreenCaptureUsageDescription</key>"
        + "<string>SheJane uses screen capture only when Computer Use is enabled.</string>"
    )
    if marker not in setup_source:
        raise RuntimeError("pinned helper Info.plist template changed")
    setup.write_text(setup_source.replace(marker, usage), encoding="utf-8")


def build_native_helper_app(upstream: Path, platform: str, output: Path) -> None:
    if platform != "darwin/arm64":
        raise RuntimeError(f"unsupported Computer Use platform: {platform}")
    shutil.rmtree(output, ignore_errors=True)
    executable = output / "Contents" / "MacOS" / "bridge"
    executable.parent.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="computer-use-swift-cache-") as cache:
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-target",
                "arm64-apple-macosx14.0",
                "-module-cache-path",
                cache,
                "-O",
                "-framework",
                "ApplicationServices",
                "-framework",
                "AppKit",
                "-framework",
                "ScreenCaptureKit",
                "-framework",
                "Foundation",
                "-framework",
                "SwiftUI",
                str(upstream / "native/macos/agent_cursor.swift"),
                str(upstream / "native/macos/agent_cursor_motion.swift"),
                str(upstream / "native/macos/bridge.swift"),
                "-o",
                str(executable),
            ],
            check=True,
        )
    info = {
        "CFBundleIdentifier": "com.injaneity.pi-computer-use",
        "CFBundleName": "pi-computer-use",
        "CFBundleDisplayName": "pi-computer-use",
        "CFBundleExecutable": "bridge",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": UPSTREAM_VERSION,
        "CFBundleVersion": UPSTREAM_VERSION,
        "LSMinimumSystemVersion": "14.0",
        "LSUIElement": True,
        "NSScreenCaptureUsageDescription": (
            "SheJane uses screen capture only when Computer Use is enabled."
        ),
    }
    with (output / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle, fmt=plistlib.FMT_XML, sort_keys=True)


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
                executable = relative.endswith("/bridge") or relative == "payload/bridge-server.mjs"
                info.external_attr = (0o500 if executable else 0o600) << 16
                archive.writestr(info, path.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
