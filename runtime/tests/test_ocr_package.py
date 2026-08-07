from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shejane_runtime.plugins.manifest import PluginManifest, load_plugin_manifest
from shejane_runtime.plugins.ocr import OCR_PLUGIN_VERSION, is_allowed_ocr_package
from shejane_runtime.plugins.package import extract_plugin_archive
from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT / "runtime" / "plugins" / "ocr"
BUILDER = ROOT / "build_package.py"
ASSET_BUILDER = ROOT / "build_runtime_asset.py"


def test_runtime_accepts_only_the_current_ocr_package_version() -> None:
    assert OCR_PLUGIN_VERSION == "0.1.4"
    assert is_allowed_ocr_package(
        plugin_id="org.shejane.ocr",
        version="0.1.4",
        handler="ocr",
    )
    assert not is_allowed_ocr_package(
        plugin_id="org.shejane.ocr",
        version="0.1.0",
        handler="ocr",
    )


def test_ocr_manifest_and_action_schemas_are_strict() -> None:
    template = (ROOT / ".shejane-plugin" / "plugin.template.json").read_text(encoding="utf-8")
    manifest = PluginManifest.model_validate_json(
        template.replace("__PLUGIN_VERSION__", "0.1.0")
        .replace("__PLATFORM__", "darwin/arm64")
        .replace("__RUNTIME_ASSET_DIGEST__", "sha256:" + "a" * 64)
    )

    assert manifest.runtime.execution.kind == "builtin"
    assert manifest.runtime.execution.handler == "ocr"
    assert manifest.runtime.execution.runtime_assets[0].id == "org.rapidocr.runtime"
    assert {action.id for action in manifest.contributions.actions} == {"ocr.recognize_images"}
    for action in manifest.contributions.actions:
        for relative in (action.input_schema, action.output_schema):
            schema = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            assert schema["additionalProperties"] is False


@pytest.mark.parametrize("target_platform", ["darwin/arm64", "windows/amd64"])
def test_ocr_package_is_deterministic_and_metadata_only(
    tmp_path: Path, target_platform: str
) -> None:
    outputs = [tmp_path / "first.shejane-plugin", tmp_path / "second.shejane-plugin"]
    for output in outputs:
        subprocess.run(
            [
                sys.executable,
                str(BUILDER),
                "--platform",
                target_platform,
                "--runtime-asset-digest",
                "sha256:" + "a" * 64,
                "--output",
                str(output),
            ],
            check=True,
        )

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    extracted = tmp_path / "extracted"
    extract_plugin_archive(outputs[0], extracted)
    manifest = load_plugin_manifest(extracted)
    assert manifest.version == "0.1.4"
    assert manifest.runtime.execution.platforms == [target_platform]
    assert not (extracted / "payload").exists()


def test_ocr_package_rejects_managed_worker_platforms(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--platform",
            "linux/arm64",
            "--runtime-asset-digest",
            "sha256:" + "a" * 64,
            "--output",
            str(tmp_path / "ocr.shejane-plugin"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "invalid choice: 'linux/arm64'" in completed.stderr


@pytest.mark.parametrize(
    ("target_platform", "engine", "worker"),
    [
        ("darwin/arm64", "ocr-engine", "ocr-worker"),
        ("windows/amd64", "ocr-engine.exe", "ocr-worker.exe"),
    ],
)
def test_ocr_runtime_asset_owns_worker_payload(
    tmp_path: Path, target_platform: str, engine: str, worker: str
) -> None:
    base_asset = tmp_path / "base.shejane-runtime-asset"
    manifest = {
        "schema_version": 1,
        "id": "org.rapidocr.runtime",
        "version": "3.9.1+ppocrv6-small.1",
        "platform": target_platform,
        "license": "Apache-2.0",
        "source_url": "https://github.com/RapidAI/RapidOCR",
        "payload": "payload",
        "sbom": ".shejane-runtime-asset/sbom.spdx.json",
        "executables": [f"payload/bin/{engine}"],
    }
    with zipfile.ZipFile(base_asset, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".shejane-runtime-asset/asset.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        )
        archive.writestr(
            ".shejane-runtime-asset/sbom.spdx.json",
            json.dumps(
                {
                    "spdxVersion": "SPDX-2.3",
                    "SPDXID": "SPDXRef-DOCUMENT",
                    "name": "rapidocr-base",
                    "documentNamespace": "https://shejane.org/spdx/test/rapidocr-base",
                    "creationInfo": {"creators": ["Organization: SheJane"]},
                    "packages": [],
                    "relationships": [],
                }
            ),
        )
        archive.writestr(f"payload/bin/{engine}", b"engine")
    worker_root = tmp_path / "worker"
    worker_root.mkdir()
    (worker_root / worker).write_bytes(b"worker")
    (worker_root / "_internal").mkdir()
    (worker_root / "_internal/runtime").write_bytes(b"python")
    outputs = [
        tmp_path / "first.shejane-runtime-asset",
        tmp_path / "second.shejane-runtime-asset",
    ]
    for output in outputs:
        subprocess.run(
            [
                sys.executable,
                str(ASSET_BUILDER),
                "--platform",
                target_platform,
                "--base-asset",
                str(base_asset),
                "--worker",
                str(worker_root),
                "--output",
                str(output),
            ],
            check=True,
        )

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    installed = RuntimeAssetStore(tmp_path / "asset-store").install(
        outputs[0], target_platform=target_platform
    )
    assert installed.version == "3.9.1+ppocrv6-small.2"
    assert installed.license == "Apache-2.0 AND AGPL-3.0-only"
    assert (installed.payload / "bin" / engine).read_bytes() == b"engine"
    assert (installed.payload / "worker" / worker).read_bytes() == b"worker"
    metadata = installed.root / ".shejane-runtime-asset"
    sbom = json.loads((metadata / "sbom.spdx.json").read_text(encoding="utf-8"))
    worker_package = next(item for item in sbom["packages"] if item["name"] == "SheJane OCR Worker")
    assert worker_package["versionInfo"] == "0.1.4"
    assert worker_package["licenseDeclared"] == "AGPL-3.0-only"
    assert (installed.root / "licenses" / "LICENSE.shejane-ocr-worker").is_file()


@pytest.mark.parametrize("unsafe_kind", ["entrypoint_symlink", "external_symlink", "fifo"])
def test_ocr_runtime_asset_rejects_unsafe_worker_entries(tmp_path: Path, unsafe_kind: str) -> None:
    if not hasattr(Path, "symlink_to") or (unsafe_kind == "fifo" and not hasattr(os, "mkfifo")):
        pytest.skip("filesystem entry type is unavailable")
    base_asset = tmp_path / "base.shejane-runtime-asset"
    manifest = {
        "schema_version": 1,
        "id": "org.rapidocr.runtime",
        "version": "3.9.1+ppocrv6-small.1",
        "platform": "darwin/arm64",
        "license": "Apache-2.0",
        "source_url": "https://github.com/RapidAI/RapidOCR",
        "payload": "payload",
        "sbom": ".shejane-runtime-asset/sbom.spdx.json",
        "executables": ["payload/bin/ocr-engine"],
    }
    with zipfile.ZipFile(base_asset, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".shejane-runtime-asset/asset.json", json.dumps(manifest))
        archive.writestr(
            ".shejane-runtime-asset/sbom.spdx.json",
            json.dumps({"packages": [], "relationships": []}),
        )
        archive.writestr("payload/bin/ocr-engine", b"engine")
    worker = tmp_path / "worker"
    worker.mkdir()
    entrypoint = worker / "ocr-worker"
    if unsafe_kind == "entrypoint_symlink":
        target = worker / "target"
        target.write_bytes(b"worker")
        entrypoint.symlink_to(target.name)
    else:
        entrypoint.write_bytes(b"worker")
        if unsafe_kind == "external_symlink":
            outside = tmp_path / "outside"
            outside.write_bytes(b"outside")
            (worker / "escape").symlink_to(outside)
        else:
            os.mkfifo(worker / "pipe")

    completed = subprocess.run(
        [
            sys.executable,
            str(ASSET_BUILDER),
            "--platform",
            "darwin/arm64",
            "--base-asset",
            str(base_asset),
            "--worker",
            str(worker),
            "--output",
            str(tmp_path / "output.shejane-runtime-asset"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "unsafe entry" in completed.stderr or "entrypoint is unavailable" in completed.stderr


def test_release_does_not_package_builtin_ocr_as_a_linux_worker() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-client.yml").read_text(
        encoding="utf-8"
    )

    assert "ocr-0.1.0-" not in workflow
    assert "ocr-0.1.4-linux-arm64.shejane-plugin" not in workflow


def test_release_publishes_browser_and_ocr_assets_outside_installers() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-client.yml").read_text(
        encoding="utf-8"
    )
    spec = (REPO_ROOT / "runtime" / "shejane-runtime.spec").read_text(encoding="utf-8")

    assert "builtin-assets" not in spec
    assert "Stage on-demand Runtime Assets outside the installer" in workflow
    assert "client/release/*.shejane-runtime-asset" in workflow


def test_release_replays_previous_frozen_runtime_data_before_publishing() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-client.yml").read_text(
        encoding="utf-8"
    )

    assert "Run previous Client Runtime data upgrade smoke" in workflow
    assert "test-packaged-runtime-upgrade.mjs" in workflow


def test_release_requires_stable_developer_id_signature() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-client.yml").read_text(
        encoding="utf-8"
    )
    builder = (REPO_ROOT / "client" / "electron-builder.yml").read_text(encoding="utf-8")

    assert "APPLE_API_KEY_P8_BASE64" in workflow
    assert "Identifier=com.shejane.runtime" in workflow
    assert "codesign -dr -" in workflow
    assert "Signature=adhoc" not in workflow
    assert "SHEJANE_CODESIGN_IDENTITY=-" not in workflow
    assert 'Developer ID Application: \\([^"]*\\)' in workflow
    assert '\\(Developer ID Application:[^"]*\\)' not in workflow
    assert "sign: ./electron/macos-sign.cjs" in builder


def test_release_packages_and_verifies_the_signed_computer_use_helper() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-client.yml").read_text(
        encoding="utf-8"
    )
    spec = (REPO_ROOT / "runtime" / "shejane-runtime.spec").read_text(encoding="utf-8")

    assert "computer-use-helper-0.2.3-darwin-arm64" in workflow
    assert "computer-use-helper/pi-computer-use.app" in spec
    assert "Identifier=com.injaneity.pi-computer-use" in workflow
    assert 'identifier "com.injaneity.pi-computer-use"' in workflow
    assert 'helper_requirement="$(codesign -dr - "${helper}" 2>&1)"' in workflow


def test_computer_use_distribution_version_is_aligned() -> None:
    for relative in (
        ".github/workflows/release-client.yml",
        "runtime/shejane-runtime.spec",
        "runtime/src/shejane_runtime/config.py",
        "runtime/plugins/computer-use/README.md",
        "scripts/build-computer-use-builtin.sh",
        "scripts/dev.sh",
    ):
        content = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "computer-use-0.2.3" in content, relative
        assert "computer-use-0.2.2" not in content, relative
    runtime = (
        REPO_ROOT / "runtime" / "src" / "shejane_runtime" / "plugins" / "computer_use.py"
    ).read_text(encoding="utf-8")
    assert 'COMPUTER_USE_PLUGIN_VERSION = "0.2.3"' in runtime


def test_ocr_distribution_version_is_aligned() -> None:
    for relative in (
        ".github/workflows/release-client.yml",
        "runtime/shejane-runtime.spec",
        "runtime/src/shejane_runtime/config.py",
        "scripts/build-ocr-builtin.sh",
        "scripts/build-ocr-windows-amd64.ps1",
        "scripts/dev.sh",
    ):
        content = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "ocr-0.1.4" in content, relative
        assert "ocr-0.1.3" not in content, relative


def test_runtime_collection_strips_native_dependencies_safely() -> None:
    spec = (REPO_ROOT / "runtime" / "shejane-runtime.spec").read_text(encoding="utf-8")
    exe_config = spec.split("exe = EXE(", 1)[1].split("coll = COLLECT(", 1)[0]
    collect_config = spec.split("coll = COLLECT(", 1)[1]

    assert "strip=" not in exe_config
    assert 'strip=sys.platform != "win32"' in collect_config
    assert "binaries.append((str(wasmtime_library)" in spec
    assert spec.index("binaries.append((str(wasmtime_library)") < spec.index("a = Analysis(")
    assert "copy2(wasmtime_library" not in spec
