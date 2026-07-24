from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import zipfile
from functools import partial
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import shejane_runtime.runs as runs_module
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.plugins.catalog import PluginCatalog
from shejane_runtime.plugins.identity import plugin_action_catalog_hash
from shejane_runtime.plugins.manifest import load_plugin_manifest
from shejane_runtime.plugins.package import canonical_package_digest, extract_plugin_archive
from shejane_runtime.plugins.platforms import current_managed_worker_execution_platform
from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app
from shejane_runtime.store.sqlite import LocalStore
from tests.helpers import run_command

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_FIXTURE = REPO_ROOT / "runtime" / "plugins" / "fixtures" / "wasi-archive"
WORKER_FIXTURE = REPO_ROOT / "runtime" / "plugins" / "fixtures" / "worker-documents"
COMPUTER_USE = REPO_ROOT / "runtime" / "plugins" / "computer-use"
AUTH = {"Authorization": "Bearer tok"}


def _pack_fixture(
    source: Path,
    destination: Path,
    *,
    version: str | None = None,
    execution_platform: str | None = None,
) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and "target" not in path.parts:
                relative = path.relative_to(source).as_posix()
                if relative == ".shejane-plugin/plugin.json" and (
                    version is not None or execution_platform is not None
                ):
                    manifest = json.loads(path.read_text())
                    if version is not None:
                        manifest["version"] = version
                    if execution_platform is not None:
                        manifest["runtime"]["execution"]["platforms"] = [execution_platform]
                    archive.writestr(relative, json.dumps(manifest))
                else:
                    archive.write(path, relative)


def _pack_runtime_asset(
    destination: Path,
    *,
    asset_id: str = "org.libreoffice.runtime",
    version: str = "25.8.7",
    platform: str | None = None,
) -> None:
    target_platform = platform or current_managed_worker_execution_platform() or "linux/arm64"
    manifest = {
        "schema_version": 1,
        "id": asset_id,
        "version": version,
        "platform": target_platform,
        "license": "MPL-2.0",
        "source_url": "https://www.libreoffice.org/",
        "payload": "payload/libreoffice",
        "sbom": ".shejane-runtime-asset/sbom.spdx.json",
    }
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".shejane-runtime-asset/asset.json",
            json.dumps(manifest),
        )
        archive.writestr(
            ".shejane-runtime-asset/sbom.spdx.json",
            json.dumps({"spdxVersion": "SPDX-2.3"}),
        )
        archive.writestr("payload/libreoffice/program.bin", "pinned")


def _pack_ocr_builtin(destination: Path, digest: str) -> None:
    manifest = {
        "schema_version": 1,
        "id": "org.shejane.ocr",
        "version": "0.1.0",
        "name": "OCR",
        "description": "Extract text from images locally.",
        "license": "Apache-2.0",
        "publisher": {"id": "org.shejane", "name": "SheJane"},
        "runtime": {
            "min_version": "0.1.0",
            "execution": {
                "kind": "builtin",
                "handler": "ocr",
                "platforms": ["darwin/arm64"],
                "runtime_assets": [
                    {
                        "id": "org.rapidocr.runtime",
                        "version": "3.9.1+ppocrv6-medium.1",
                        "digest": digest,
                    }
                ],
            },
        },
        "contributions": {
            "actions": [
                {
                    "id": "ocr.recognize_images",
                    "title": "Recognize image text",
                    "description": "Recognize text in selected images.",
                    "input_schema": "actions/input.json",
                    "output_schema": "actions/output.json",
                    "consumes": ["image/png"],
                    "produces": ["text/plain", "application/json"],
                    "effects": ["read", "artifact"],
                    "determinism": "input_stable",
                    "capabilities": ["input.read", "artifact.write"],
                    "limits": {"timeout_ms": 300000, "memory_mb": 2048, "output_mb": 64},
                }
            ],
            "commands": [],
        },
    }
    schema = {"type": "object", "additionalProperties": True}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".shejane-plugin/plugin.json", json.dumps(manifest))
        archive.writestr("actions/input.json", json.dumps(schema))
        archive.writestr("actions/output.json", json.dumps(schema))
        archive.writestr("payload/ocr-worker", "#!/bin/sh\nexit 0\n")


def _pack_worker_with_runtime_asset(destination: Path, digest: str) -> None:
    platform = current_managed_worker_execution_platform() or "linux/arm64"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(WORKER_FIXTURE.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(WORKER_FIXTURE).as_posix()
            if relative == ".shejane-plugin/plugin.json":
                manifest = json.loads(path.read_text())
                execution = manifest["runtime"]["execution"]
                execution["platforms"] = [platform]
                execution["runtime_assets"] = [
                    {
                        "id": "org.libreoffice.runtime",
                        "version": "25.8.7",
                        "digest": digest,
                    }
                ]
                archive.writestr(relative, json.dumps(manifest))
            else:
                archive.write(path, relative)


def _pack_computer_use_builtin(destination: Path) -> None:
    manifest = (COMPUTER_USE / ".shejane-plugin" / "plugin.template.json").read_text()
    manifest = manifest.replace("__PLUGIN_VERSION__", "0.2.0").replace(
        "__PLATFORM__", "darwin/arm64"
    )
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".shejane-plugin/plugin.json", manifest)
        for folder in ("actions", "commands"):
            for path in sorted((COMPUTER_USE / folder).rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(COMPUTER_USE).as_posix())
        archive.writestr("payload/bridge-server.mjs", "process.exit(0)\n")


def _pack_browser_qa_builtin(
    destination: Path,
    digest: str,
    *,
    bridge_source: str = "process.exit(0)\n",
) -> None:
    manifest = {
        "schema_version": 1,
        "id": "org.shejane.browser-qa",
        "version": "0.1.0",
        "name": "Browser QA",
        "description": "Open, operate, and inspect web pages in an isolated SheJane browser.",
        "license": "Apache-2.0",
        "publisher": {"id": "org.shejane", "name": "SheJane"},
        "runtime": {
            "min_version": "0.1.0",
            "execution": {
                "kind": "builtin",
                "handler": "browser_qa",
                "platforms": ["darwin/arm64"],
                "runtime_assets": [
                    {
                        "id": "org.shejane.browser-qa.runtime",
                        "version": "1.61.1+chromium1228.1",
                        "digest": digest,
                    }
                ],
            },
        },
        "contributions": {
            "actions": [
                {
                    "id": "open",
                    "title": "Open page",
                    "description": "Open one public HTTP or HTTPS page.",
                    "input_schema": "actions/open.input.json",
                    "output_schema": "actions/result.output.json",
                    "consumes": [],
                    "produces": [],
                    "effects": ["read", "external"],
                    "determinism": "nondeterministic",
                    "capabilities": ["browser.observe"],
                    "limits": {"timeout_ms": 60000, "memory_mb": 512, "output_mb": 8},
                }
            ],
            "commands": [],
        },
    }
    schema = {"type": "object", "additionalProperties": True}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".shejane-plugin/plugin.json", json.dumps(manifest))
        archive.writestr("actions/open.input.json", json.dumps(schema))
        archive.writestr("actions/result.output.json", json.dumps(schema))
        archive.writestr("payload/bridge-server.mjs", bridge_source)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path / "runtime",
        computer_use_package=None,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_plugin_source_api_and_commands_are_not_exposed(client: TestClient) -> None:
    assert client.get("/v1/plugin-sources", headers=AUTH).status_code == 404

    response = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.source.add",
            "command_id": "cmd_removed_plugin_source",
            "index_url": "https://example.test/index.json",
            "signature_url": "https://example.test/index.sig.json",
            "public_key": "a" * 44,
        },
    )

    assert response.status_code == 422


def test_computer_use_is_runtime_managed_and_cannot_be_installed_or_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "computer-use.shejane-plugin"
    _pack_computer_use_builtin(package)
    monkeypatch.setattr(
        "shejane_runtime.plugins.registry.current_managed_worker_platform",
        lambda: "darwin/arm64",
    )
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path / "runtime",
        computer_use_package=package,
    )
    with TestClient(create_app(settings)) as builtin_client:
        listed = builtin_client.get("/v1/plugins", headers=AUTH)
        assert listed.status_code == 200, listed.text
        plugin = listed.json()["plugins"][0]
        assert plugin["id"] == "org.shejane.computer-use"
        assert plugin["execution_kind"] == "builtin"
        assert plugin["enabled"] is False

        install = builtin_client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.install",
                "command_id": "cmd_install_builtin_again",
                "source_path": str(package),
                "allow_unsigned": True,
            },
        )
        assert install.status_code == 409
        assert install.json()["detail"]["code"] == "builtin_capability_managed"

        remove = builtin_client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.remove",
                "command_id": "cmd_remove_builtin",
                "plugin_id": "org.shejane.computer-use",
                "expected_digest": plugin["digest"],
            },
        )
        assert remove.status_code == 409
        assert remove.json()["detail"]["code"] == "builtin_capability_managed"


def test_browser_qa_is_runtime_managed_and_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = tmp_path / "browser-qa.shejane-runtime-asset"
    _pack_runtime_asset(
        asset,
        asset_id="org.shejane.browser-qa.runtime",
        version="1.61.1+chromium1228.1",
        platform="darwin/arm64",
    )
    digest = (
        RuntimeAssetStore(tmp_path / "browser-asset-store")
        .install(asset, target_platform="darwin/arm64")
        .digest
    )
    package = tmp_path / "browser-qa.shejane-plugin"
    _pack_browser_qa_builtin(package, digest)
    monkeypatch.setattr(
        "shejane_runtime.plugins.registry.current_managed_worker_platform",
        lambda: "darwin/arm64",
    )
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path / "runtime",
        computer_use_package=None,
        browser_qa_package=package,
        browser_qa_runtime_asset=asset,
    )
    with TestClient(create_app(settings)) as builtin_client:
        listed = builtin_client.get("/v1/plugins", headers=AUTH)
        assert listed.status_code == 200, listed.text
        plugin = listed.json()["plugins"][0]
        assert plugin["id"] == "org.shejane.browser-qa"
        assert plugin["execution_kind"] == "builtin"
        assert plugin["enabled"] is False

        package_root = (
            settings.data_dir / "plugins" / "packages" / plugin["digest"].removeprefix("sha256:")
        )
        manifest = load_plugin_manifest(package_root).model_dump(mode="json")

        async def verify_catalog_asset() -> None:
            binding = {
                "plugin_id": plugin["id"],
                "version": plugin["version"],
                "digest": plugin["digest"],
                "action_catalog_hash": plugin_action_catalog_hash(
                    manifest, plugin_digest=plugin["digest"]
                ),
            }
            async with PluginCatalog(settings.data_dir).acquire_snapshot(
                [binding], execution_context=object()
            ) as lease:
                assert lease.actions[0].runtime_assets[0].asset_id == (
                    "org.shejane.browser-qa.runtime"
                )

        asyncio.run(verify_catalog_asset())

        remove = builtin_client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.remove",
                "command_id": "cmd_remove_browser_qa",
                "plugin_id": "org.shejane.browser-qa",
                "expected_digest": plugin["digest"],
            },
        )
        assert remove.status_code == 409
        assert remove.json()["detail"]["code"] == "builtin_capability_managed"


def test_plugin_list_reads_registry_without_activating_fixed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = tmp_path / "browser-qa.shejane-runtime-asset"
    _pack_runtime_asset(
        asset,
        asset_id="org.shejane.browser-qa.runtime",
        version="1.61.1+chromium1228.1",
        platform="darwin/arm64",
    )
    digest = (
        RuntimeAssetStore(tmp_path / "asset-store")
        .install(asset, target_platform="darwin/arm64")
        .digest
    )
    package = tmp_path / "browser-qa.shejane-plugin"
    activation_marker = tmp_path / "browser-qa-started"
    _pack_browser_qa_builtin(
        package,
        digest,
        bridge_source=(
            "import { writeFileSync } from 'node:fs';\n"
            f"writeFileSync({json.dumps(str(activation_marker))}, 'started');\n"
        ),
    )
    monkeypatch.setattr(
        "shejane_runtime.plugins.registry.current_managed_worker_platform",
        lambda: "darwin/arm64",
    )
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path / "runtime",
        computer_use_package=None,
        browser_qa_package=package,
        browser_qa_runtime_asset=asset,
    )

    with TestClient(create_app(settings)) as builtin_client:
        package.unlink()
        asset.unlink()

        listed = builtin_client.get("/v1/plugins", headers=AUTH)

    assert listed.status_code == 200, listed.text
    assert [plugin["id"] for plugin in listed.json()["plugins"]] == ["org.shejane.browser-qa"]
    assert not activation_marker.exists()


def test_ocr_asset_and_plugin_are_runtime_managed_and_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = tmp_path / "rapidocr.shejane-runtime-asset"
    _pack_runtime_asset(
        asset,
        asset_id="org.rapidocr.runtime",
        version="3.9.1+ppocrv6-medium.1",
        platform="darwin/arm64",
    )
    digest = (
        RuntimeAssetStore(tmp_path / "asset-store")
        .install(asset, target_platform="darwin/arm64")
        .digest
    )
    package = tmp_path / "ocr.shejane-plugin"
    _pack_ocr_builtin(package, digest)
    monkeypatch.setattr(
        "shejane_runtime.plugins.registry.current_managed_worker_platform",
        lambda: "darwin/arm64",
    )
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path / "runtime",
        computer_use_package=None,
        ocr_runtime_asset=asset,
        ocr_package=package,
    )
    with TestClient(create_app(settings)) as builtin_client:
        listed = builtin_client.get("/v1/plugins", headers=AUTH)
        assert listed.status_code == 200, listed.text
        plugin = listed.json()["plugins"][0]
        assert plugin["id"] == "org.shejane.ocr"
        assert plugin["execution_kind"] == "builtin"

        remove = builtin_client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.remove",
                "command_id": "cmd_remove_ocr",
                "plugin_id": "org.shejane.ocr",
                "expected_digest": plugin["digest"],
            },
        )
        assert remove.status_code == 409
        assert remove.json()["detail"]["code"] == "builtin_capability_managed"


def test_install_wasi_plugin_is_idempotent_and_lists_from_runtime_store(
    client: TestClient,
    tmp_path: Path,
) -> None:
    package = tmp_path / "archive.shejane-plugin"
    _pack_fixture(ARCHIVE_FIXTURE, package)
    command = {
        "type": "plugin.install",
        "command_id": "cmd_plugin_install_archive",
        "source_path": str(package),
        "allow_unsigned": True,
    }

    first = client.post("/v1/commands", headers=AUTH, json=command)
    assert first.status_code == 200, first.text
    receipt = first.json()
    assert receipt["type"] == "plugin.install"
    assert receipt["plugin_id"] == "dev.shejane.fixture.archive"
    assert receipt["version"] == "0.1.0"
    assert receipt["digest"].startswith("sha256:")
    assert receipt["installed"] is True
    assert receipt["enabled"] is False

    replay = client.post("/v1/commands", headers=AUTH, json=command)
    assert replay.status_code == 200
    assert replay.json() == receipt

    duplicate = client.post(
        "/v1/commands",
        headers=AUTH,
        json={**command, "command_id": "cmd_plugin_install_archive_again"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["digest"] == receipt["digest"]
    assert len(list((tmp_path / "runtime" / "plugins" / "packages").iterdir())) == 1

    package.unlink()
    listed = client.get("/v1/plugins", headers=AUTH)
    assert listed.status_code == 200
    assert listed.json() == {
        "plugins": [
            {
                "id": "dev.shejane.fixture.archive",
                "name": "Archive fixture",
                "description": "Reference WASI plugin that extracts an archive into staged artifacts.",
                "version": "0.1.0",
                "digest": receipt["digest"],
                "publisher": {"id": "dev.shejane", "name": "SheJane"},
                "execution_kind": "wasi",
                "signature_status": "unsigned",
                "compatibility": "compatible",
                "enabled": False,
                "retired": False,
            }
        ]
    }


def test_install_requires_unsigned_confirmation(client: TestClient, tmp_path: Path) -> None:
    package = tmp_path / "archive.shejane-plugin"
    _pack_fixture(ARCHIVE_FIXTURE, package)

    response = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_unsigned_without_confirmation",
            "source_path": str(package),
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unsigned_plugin_confirmation_required"
    assert client.get("/v1/plugins", headers=AUTH).json() == {"plugins": []}


def test_install_rejects_managed_worker_without_os_sandbox(
    client: TestClient,
    tmp_path: Path,
) -> None:
    package = tmp_path / "documents.shejane-plugin"
    _pack_fixture(
        WORKER_FIXTURE,
        package,
        execution_platform=current_managed_worker_execution_platform() or "linux/arm64",
    )

    response = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_worker_without_sandbox",
            "source_path": str(package),
            "allow_unsigned": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "managed_worker_sandbox_unavailable"
    assert client.get("/v1/plugins", headers=AUTH).json() == {"plugins": []}


def test_install_rejects_managed_worker_for_another_platform(
    client: TestClient,
    tmp_path: Path,
) -> None:
    package = tmp_path / "documents-wrong-platform.shejane-plugin"
    current = current_managed_worker_execution_platform()
    wrong = next(
        candidate
        for candidate in (
            "darwin/arm64",
            "darwin/amd64",
            "linux/arm64",
            "linux/amd64",
            "windows/arm64",
            "windows/amd64",
        )
        if candidate != current
    )
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(WORKER_FIXTURE.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(WORKER_FIXTURE).as_posix()
            if relative == ".shejane-plugin/plugin.json":
                manifest = json.loads(path.read_text())
                manifest["runtime"]["execution"]["platforms"] = [wrong]
                archive.writestr(relative, json.dumps(manifest))
            else:
                archive.write(path, relative)

    response = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_worker_wrong_platform",
            "source_path": str(package),
            "allow_unsigned": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "plugin_platform_incompatible"


def test_runtime_asset_install_is_content_addressed_and_idempotent(
    client: TestClient,
    tmp_path: Path,
) -> None:
    asset = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack_runtime_asset(asset)
    command = {
        "type": "plugin.runtime_asset.install",
        "command_id": "cmd_runtime_asset_install",
        "source_path": str(asset),
    }

    first = client.post("/v1/commands", headers=AUTH, json=command)
    replay = client.post("/v1/commands", headers=AUTH, json=command)

    assert first.status_code == 200, first.text
    assert replay.json() == first.json()
    assert first.json() == {
        "type": "plugin.runtime_asset.install",
        "command_id": "cmd_runtime_asset_install",
        "asset_id": "org.libreoffice.runtime",
        "version": "25.8.7",
        "platform": current_managed_worker_execution_platform(),
        "digest": first.json()["digest"],
        "installed": True,
    }
    assert first.json()["digest"].startswith("sha256:")


def test_runtime_asset_install_rejects_expected_digest_mismatch(
    client: TestClient,
    tmp_path: Path,
) -> None:
    asset = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack_runtime_asset(asset)

    response = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.runtime_asset.install",
            "command_id": "cmd_runtime_asset_wrong_digest",
            "source_path": str(asset),
            "expected_digest": "sha256:" + "0" * 64,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_runtime_asset"


def test_managed_worker_requires_its_exact_runtime_asset_before_sandbox_gate(
    client: TestClient,
    tmp_path: Path,
) -> None:
    asset = tmp_path / "libreoffice.shejane-runtime-asset"
    _pack_runtime_asset(asset)
    installed_asset = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.runtime_asset.install",
            "command_id": "cmd_install_worker_asset",
            "source_path": str(asset),
        },
    ).json()
    worker = tmp_path / "documents-with-asset.shejane-plugin"
    _pack_worker_with_runtime_asset(worker, installed_asset["digest"])

    available = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_worker_asset_available",
            "source_path": str(worker),
            "allow_unsigned": True,
        },
    )

    assert available.status_code == 409
    assert available.json()["detail"]["code"] == "managed_worker_sandbox_unavailable"

    missing_worker = tmp_path / "documents-missing-asset.shejane-plugin"
    _pack_worker_with_runtime_asset(missing_worker, "sha256:" + "f" * 64)
    missing = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_worker_asset_missing",
            "source_path": str(missing_worker),
            "allow_unsigned": True,
        },
    )

    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "plugin_runtime_asset_unavailable"


def test_install_rejects_same_version_with_different_digest(
    client: TestClient,
    tmp_path: Path,
) -> None:
    first = tmp_path / "archive-first.shejane-plugin"
    second = tmp_path / "archive-second.shejane-plugin"
    _pack_fixture(ARCHIVE_FIXTURE, first)
    _pack_fixture(ARCHIVE_FIXTURE, second)
    with zipfile.ZipFile(second, "a") as package:
        package.writestr("payload/different-content", "different")

    accepted = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_install_first_digest",
            "source_path": str(first),
            "allow_unsigned": True,
        },
    )
    rejected = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_install_changed_digest",
            "source_path": str(second),
            "allow_unsigned": True,
        },
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "plugin_version_conflict"
    assert (
        client.get("/v1/plugins", headers=AUTH).json()["plugins"][0]["digest"]
        == (accepted.json()["digest"])
    )
    assert len(list((tmp_path / "runtime" / "plugins" / "packages").iterdir())) == 1


def test_plugin_detail_enable_and_disable_are_runtime_owned_commands(
    client: TestClient,
    tmp_path: Path,
) -> None:
    package = tmp_path / "archive.shejane-plugin"
    _pack_fixture(ARCHIVE_FIXTURE, package)
    installed = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_install_for_enable",
            "source_path": str(package),
            "allow_unsigned": True,
        },
    ).json()

    detail = client.get(
        "/v1/plugins/dev.shejane.fixture.archive",
        headers=AUTH,
    )
    assert detail.status_code == 200
    assert detail.json()["description"].startswith("Reference WASI plugin")
    assert detail.json()["actions"] == [
        {
            "id": "archive.extract",
            "title": "Extract archive",
            "description": "Extract an authorized ZIP input into the staged output directory.",
            "consumes": ["application/zip"],
            "produces": ["application/octet-stream"],
            "effects": ["read", "artifact"],
            "determinism": "input_stable",
            "capabilities": ["input.read", "artifact.write"],
            "limits": {"timeout_ms": 10000, "memory_mb": 128, "output_mb": 8},
        }
    ]
    assert detail.json()["commands"] == []
    assert detail.json()["skills"] == []
    assert detail.json()["mcp_servers"] == []

    enable_command = {
        "type": "plugin.enable",
        "command_id": "cmd_enable_archive",
        "plugin_id": "dev.shejane.fixture.archive",
        "expected_digest": installed["digest"],
    }
    enabled = client.post("/v1/commands", headers=AUTH, json=enable_command)
    replay = client.post("/v1/commands", headers=AUTH, json=enable_command)
    assert enabled.status_code == 200
    assert replay.json() == enabled.json()
    assert enabled.json() == {
        "type": "plugin.enable",
        "command_id": "cmd_enable_archive",
        "plugin_id": "dev.shejane.fixture.archive",
        "digest": installed["digest"],
        "enabled": True,
    }
    assert client.get("/v1/plugins", headers=AUTH).json()["plugins"][0]["enabled"] is True

    disabled = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.disable",
            "command_id": "cmd_disable_archive",
            "plugin_id": "dev.shejane.fixture.archive",
        },
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert client.get("/v1/plugins", headers=AUTH).json()["plugins"][0]["enabled"] is False


def test_vision_model_binding_is_validated_and_frozen_per_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def configured_key(*_args, **_kwargs):
        return "test-key"

    monkeypatch.setattr(runs_module, "get_model_api_key", configured_key)
    with TestClient(create_app(settings)) as client:
        manifest = json.loads((WORKER_FIXTURE / ".shejane-plugin" / "plugin.json").read_text())
        manifest["contributions"]["actions"][0]["capabilities"].append("model.vision.invoke")
        plugin_digest = "sha256:" + "a" * 64
        client.portal.call(
            partial(
                client.app.state.store.install_plugin_command,
                principal_id="local:owner",
                command_id="seed_vision_plugin",
                command_payload={"type": "plugin.install", "source_path": "test"},
                manifest=manifest,
                digest=plugin_digest,
                signature_status="unsigned",
                signer_key_id=None,
                compatibility="compatible",
                source="test",
            )
        )
        client.portal.call(
            partial(
                client.app.state.store.create_model_connection,
                principal_id="local:owner",
                connection_id="vision",
                preset_id="custom",
                name="Vision service",
                region="custom",
                adapter_id="openai_chat",
                base_url="http://127.0.0.1:11434/v1",
                requires_api_key=True,
                credential_ref="keyring:model-service:vision",
                models=[
                    {
                        "model_id": model_id,
                        "display_name": display_name,
                        "source": "manual",
                        "verification": "verified",
                        "recommended": False,
                        "tool_calling": True,
                        "streaming": True,
                        "image_inputs": True,
                    }
                    for model_id, display_name in (
                        ("vision-a", "Vision A"),
                        ("vision-b", "Vision B"),
                    )
                ],
                catalog_status="ready",
            )
        )

        unconfigured = client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.enable",
                "command_id": "enable_unconfigured_vision",
                "plugin_id": manifest["id"],
            },
        )
        assert unconfigured.status_code == 409
        assert unconfigured.json()["detail"]["code"] == "plugin_model_binding_required"

        first = client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.model.bind",
                "command_id": "bind_vision_a",
                "plugin_id": manifest["id"],
                "expected_digest": plugin_digest,
                "binding_id": "vision-default",
                "model": "local:vision:vision-a",
            },
        )
        assert first.status_code == 200, first.text
        assert first.json()["model_binding"] == {
            "id": "vision-default",
            "requested_model": "local:vision:vision-a",
            "connection_id": "vision",
            "connection_version": 1,
            "model_id": "vision-a",
        }
        enabled = client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.enable",
                "command_id": "enable_seeded_vision",
                "plugin_id": manifest["id"],
            },
        )
        assert enabled.status_code == 200, enabled.text
        accepted = client.post(
            "/v1/runs",
            headers=AUTH,
            json=run_command("analyze an image", model="local:vision:vision-a"),
        )
        assert accepted.status_code == 200, accepted.text

        second = client.post(
            "/v1/commands",
            headers=AUTH,
            json={
                "type": "plugin.model.bind",
                "command_id": "bind_vision_b",
                "plugin_id": manifest["id"],
                "binding_id": "vision-default",
                "model": "local:vision:vision-b",
            },
        )
        assert second.status_code == 200, second.text
        bindings = client.portal.call(
            client.app.state.store.list_run_plugin_bindings,
            accepted.json()["id"],
        )

        assert bindings[0]["model_binding"]["model_id"] == "vision-a"
        detail = client.get(f"/v1/plugins/{manifest['id']}", headers=AUTH).json()
        assert detail["model_binding"]["model_id"] == "vision-b"


@pytest.mark.asyncio
async def test_existing_command_table_migrates_to_non_run_commands(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE local_commands ("
            "principal_id TEXT NOT NULL, id TEXT NOT NULL, command_type TEXT NOT NULL, "
            "client_message_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "response_json TEXT NOT NULL DEFAULT '{}', run_id TEXT NOT NULL, "
            "created_at TEXT NOT NULL, PRIMARY KEY (principal_id, id))"
        )

    store = await LocalStore.open(db_path)
    await store.close()
    with sqlite3.connect(db_path) as conn:
        columns = conn.execute("PRAGMA table_info(local_commands)").fetchall()

    run_id = next(column for column in columns if column[1] == "run_id")
    assert run_id[3] == 0


def test_update_rollback_remove_and_reinstall_preserve_version_history(
    client: TestClient,
    tmp_path: Path,
) -> None:
    v1 = tmp_path / "archive-v1.shejane-plugin"
    v2 = tmp_path / "archive-v2.shejane-plugin"
    _pack_fixture(ARCHIVE_FIXTURE, v1)
    _pack_fixture(ARCHIVE_FIXTURE, v2, version="0.2.0")
    installed = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_lifecycle_install",
            "source_path": str(v1),
            "allow_unsigned": True,
        },
    ).json()
    client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.enable",
            "command_id": "cmd_lifecycle_enable",
            "plugin_id": installed["plugin_id"],
        },
    )

    updated = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.update",
            "command_id": "cmd_lifecycle_update",
            "plugin_id": installed["plugin_id"],
            "source_path": str(v2),
            "expected_digest": installed["digest"],
            "allow_unsigned": True,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == "0.2.0"
    assert updated.json()["previous_digest"] == installed["digest"]
    assert updated.json()["enabled"] is True

    versions = client.get(
        "/v1/plugins/dev.shejane.fixture.archive",
        headers=AUTH,
    ).json()["versions"]
    assert [version["version"] for version in versions] == ["0.2.0", "0.1.0"]
    assert [version["active"] for version in versions] == [True, False]
    assert versions[1]["digest"] == installed["digest"]

    rolled_back = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.rollback",
            "command_id": "cmd_lifecycle_rollback",
            "plugin_id": installed["plugin_id"],
            "target_digest": installed["digest"],
            "expected_digest": updated.json()["digest"],
        },
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["version"] == "0.1.0"
    assert rolled_back.json()["digest"] == installed["digest"]

    removed = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.remove",
            "command_id": "cmd_lifecycle_remove",
            "plugin_id": installed["plugin_id"],
            "expected_digest": installed["digest"],
        },
    )
    assert removed.status_code == 200
    assert removed.json()["retired"] is True
    listed = client.get("/v1/plugins", headers=AUTH).json()["plugins"][0]
    assert listed["enabled"] is False
    assert listed["retired"] is True

    reinstalled = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_lifecycle_reinstall",
            "source_path": str(v1),
            "allow_unsigned": True,
        },
    )
    assert reinstalled.status_code == 200
    listed = client.get("/v1/plugins", headers=AUTH).json()["plugins"][0]
    assert listed["enabled"] is False
    assert listed["retired"] is False
    assert len(list((tmp_path / "runtime" / "plugins" / "packages").iterdir())) == 2


def test_signed_install_uses_deployment_trust_store_and_rejects_revoked_key(
    client: TestClient,
    tmp_path: Path,
) -> None:
    unsigned = tmp_path / "unsigned.shejane-plugin"
    signed = tmp_path / "signed.shejane-plugin"
    package_root = tmp_path / "signed-root"
    _pack_fixture(ARCHIVE_FIXTURE, unsigned)
    extract_plugin_archive(unsigned, package_root)
    digest = canonical_package_digest(package_root)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    key_id = "ed25519:sha256:" + hashlib.sha256(public_key).hexdigest()
    signature = private_key.sign(b"shejane-plugin-signature-v1\0" + digest.encode())
    signature_path = package_root / ".shejane-plugin" / "signature.json"
    signature_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "key_id": key_id,
                "package_digest": digest,
                "signature": base64.b64encode(signature).decode(),
            }
        )
    )
    _pack_fixture(package_root, signed)
    trust_store_path = tmp_path / "runtime" / "plugins" / "trusted-publishers.json"
    trust_store_path.parent.mkdir(parents=True, exist_ok=True)

    def write_trust(status: str) -> None:
        trust_store_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "keys": [
                        {
                            "publisher_id": "dev.shejane",
                            "key_id": key_id,
                            "public_key": base64.b64encode(public_key).decode(),
                            "status": status,
                        }
                    ],
                }
            )
        )

    write_trust("trusted")
    accepted = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_signed_install",
            "source_path": str(signed),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert (
        client.get("/v1/plugins", headers=AUTH).json()["plugins"][0]["signature_status"]
        == "verified"
    )

    write_trust("revoked")
    rejected = client.post(
        "/v1/commands",
        headers=AUTH,
        json={
            "type": "plugin.install",
            "command_id": "cmd_signed_install_after_revoke",
            "source_path": str(signed),
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "plugin_signer_revoked"
