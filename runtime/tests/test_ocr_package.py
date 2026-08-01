from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shejane_runtime.plugins.manifest import PluginManifest, load_plugin_manifest
from shejane_runtime.plugins.ocr import is_allowed_ocr_package
from shejane_runtime.plugins.package import extract_plugin_archive

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT / "runtime" / "plugins" / "ocr"
BUILDER = ROOT / "build_package.py"


def test_runtime_accepts_only_the_current_ocr_package_version() -> None:
    assert is_allowed_ocr_package(
        plugin_id="org.shejane.ocr",
        version="0.1.2",
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


@pytest.mark.parametrize(
    ("target_platform", "entrypoint", "library"),
    [
        ("darwin/arm64", "ocr-worker", "libpython.so"),
        ("windows/amd64", "ocr-worker.exe", "python312.dll"),
    ],
)
def test_ocr_package_is_deterministic_and_preserves_onedir_worker(
    tmp_path: Path,
    target_platform: str,
    entrypoint: str,
    library: str,
) -> None:
    worker = tmp_path / "ocr-worker"
    worker.mkdir()
    (worker / entrypoint).write_bytes(b"worker")
    (worker / "_internal").mkdir()
    (worker / "_internal" / library).write_bytes(b"library")
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
                "--worker",
                str(worker),
                "--output",
                str(output),
            ],
            check=True,
        )

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    extracted = tmp_path / "extracted"
    extract_plugin_archive(outputs[0], extracted)
    manifest = load_plugin_manifest(extracted)
    assert manifest.version == "0.1.2"
    assert manifest.runtime.execution.platforms == [target_platform]
    assert (extracted / "payload" / entrypoint).read_bytes() == b"worker"
    assert (extracted / "payload/_internal" / library).read_bytes() == b"library"


def test_ocr_package_rejects_managed_worker_platforms(tmp_path: Path) -> None:
    worker = tmp_path / "ocr-worker"
    worker.mkdir()
    (worker / "ocr-worker").write_bytes(b"worker")

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--platform",
            "linux/arm64",
            "--runtime-asset-digest",
            "sha256:" + "a" * 64,
            "--worker",
            str(worker),
            "--output",
            str(tmp_path / "ocr.shejane-plugin"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "invalid choice: 'linux/arm64'" in completed.stderr


def test_ocr_package_materializes_safe_macos_framework_links(tmp_path: Path) -> None:
    worker = tmp_path / "ocr-worker"
    worker.mkdir()
    (worker / "ocr-worker").write_bytes(b"worker")
    framework = worker / "_internal" / "Python.framework"
    version = framework / "Versions" / "3.12"
    version.mkdir(parents=True)
    (version / "Python").write_bytes(b"python-runtime")
    (framework / "Versions" / "Current").symlink_to("3.12", target_is_directory=True)
    (framework / "Python").symlink_to("Versions/Current/Python")
    output = tmp_path / "ocr.shejane-plugin"

    subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--platform",
            "darwin/arm64",
            "--runtime-asset-digest",
            "sha256:" + "a" * 64,
            "--worker",
            str(worker),
            "--output",
            str(output),
        ],
        check=True,
    )

    extracted = tmp_path / "extracted"
    extract_plugin_archive(output, extracted)
    copied_framework = extracted / "payload" / "_internal" / "Python.framework"
    assert not any(path.is_symlink() for path in copied_framework.rglob("*"))
    assert (copied_framework / "Python").read_bytes() == b"python-runtime"
    assert (copied_framework / "Versions" / "Current" / "Python").read_bytes() == (
        b"python-runtime"
    )


def test_ocr_package_rejects_directory_link_outside_worker(tmp_path: Path) -> None:
    worker = tmp_path / "ocr-worker"
    worker.mkdir()
    (worker / "ocr-worker").write_bytes(b"worker")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (worker / "_internal").symlink_to(outside, target_is_directory=True)

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--platform",
            "darwin/arm64",
            "--runtime-asset-digest",
            "sha256:" + "a" * 64,
            "--worker",
            str(worker),
            "--output",
            str(tmp_path / "ocr.shejane-plugin"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--worker contains an unsafe entry" in completed.stderr


def test_release_does_not_package_builtin_ocr_as_a_linux_worker() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-client.yml").read_text(
        encoding="utf-8"
    )

    assert "ocr-0.1.0-" not in workflow
    assert "ocr-0.1.2-linux-arm64.shejane-plugin" not in workflow
    assert "Run Linux arm64 OCR production gate in packaged macOS VM" in workflow
