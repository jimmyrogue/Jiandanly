from __future__ import annotations

import base64
import hashlib
import stat
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from shejane_runtime.plugins import platforms as plugin_platforms
from shejane_runtime.plugins.package import (
    InvalidPluginPackage,
    InvalidPluginSignature,
    canonical_package_digest,
    extract_plugin_archive,
    verify_package_signature,
)
from shejane_runtime.plugins.platforms import (
    current_managed_worker_execution_platform,
    current_managed_worker_platform,
    prepare_managed_worker_entrypoint,
)


def _package(root: Path, *, reverse_order: bool = False) -> None:
    files = [
        (".shejane-plugin/plugin.json", '{"schema_version":1}\n'),
        ("payload/action.bin", "fixture payload\n"),
    ]
    for relative, content in reversed(files) if reverse_order else files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_canonical_package_digest_ignores_creation_order_and_detached_signature(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _package(first)
    _package(second, reverse_order=True)

    first_digest = canonical_package_digest(first)
    signature = first / ".shejane-plugin" / "signature.json"
    signature.write_text('{"signature":"changes independently"}\n')

    assert first_digest == canonical_package_digest(first)
    assert first_digest == canonical_package_digest(second)
    (second / "payload" / "action.bin").write_text("tampered\n")
    assert first_digest != canonical_package_digest(second)


def test_package_signature_binds_digest_to_supplied_ed25519_key(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package)
    digest = canonical_package_digest(package)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    key_id = "ed25519:sha256:" + hashlib.sha256(public_key).hexdigest()
    signature = private_key.sign(b"shejane-plugin-signature-v1\0" + digest.encode())
    envelope = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "key_id": key_id,
        "package_digest": digest,
        "signature": base64.b64encode(signature).decode(),
    }

    assert verify_package_signature(package, envelope, public_key) == key_id
    (package / "payload" / "action.bin").write_text("tampered\n")
    with pytest.raises(InvalidPluginSignature):
        verify_package_signature(package, envelope, public_key)


def test_canonical_package_digest_rejects_links(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package)
    (package / "payload" / "link").symlink_to(package / "payload" / "action.bin")

    with pytest.raises(InvalidPluginPackage):
        canonical_package_digest(package)


def test_archive_extraction_rejects_path_traversal_without_writing_outside_staging(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "bad.shejane-plugin"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(".shejane-plugin/plugin.json", "{}")
        package.writestr("../escape", "bad")

    with pytest.raises(InvalidPluginPackage):
        extract_plugin_archive(archive, tmp_path / "staging")
    assert not (tmp_path / "escape").exists()


def test_archive_extraction_rejects_absolute_links_and_case_collisions(tmp_path: Path) -> None:
    archives = []
    for name in ("absolute", "link", "collision"):
        path = tmp_path / f"{name}.shejane-plugin"
        archives.append(path)
        with zipfile.ZipFile(path, "w") as package:
            package.writestr(".shejane-plugin/plugin.json", "{}")
            if name == "absolute":
                package.writestr("/payload", "bad")
            elif name == "link":
                link = zipfile.ZipInfo("payload/link")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(link, "target")
            else:
                package.writestr("payload/Action", "one")
                package.writestr("payload/action", "two")

    for index, archive in enumerate(archives):
        with pytest.raises(InvalidPluginPackage):
            extract_plugin_archive(archive, tmp_path / f"staging-{index}")


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("darwin", "arm64", "darwin/arm64"),
        ("darwin", "x86_64", "darwin/amd64"),
        ("linux", "aarch64", "linux/arm64"),
        ("linux", "AMD64", "linux/amd64"),
        ("win32", "ARM64", "windows/arm64"),
        ("cygwin", "x86_64", None),
        ("linux", "riscv64", None),
    ],
)
def test_current_managed_worker_platform_is_canonical(
    system: str,
    machine: str,
    expected: str | None,
) -> None:
    assert current_managed_worker_platform(system=system, machine=machine) == expected


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("darwin", "arm64", "linux/arm64"),
        ("darwin", "x86_64", "linux/amd64"),
        ("linux", "aarch64", "linux/arm64"),
        ("win32", "AMD64", "linux/amd64"),
        ("cygwin", "x86_64", None),
    ],
)
def test_current_managed_worker_execution_platform_matches_backend_abi(
    system: str,
    machine: str,
    expected: str | None,
) -> None:
    assert current_managed_worker_execution_platform(system=system, machine=machine) == expected


def test_runtime_sets_only_the_declared_worker_entrypoint_executable(tmp_path: Path) -> None:
    package = tmp_path / "package"
    entrypoint = package / "payload" / "worker"
    sibling = package / "payload" / "library.bin"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b"worker")
    sibling.write_bytes(b"library")
    entrypoint.chmod(0o644)
    sibling.chmod(0o644)

    prepare_managed_worker_entrypoint(package, "payload/worker")

    assert stat.S_IMODE(entrypoint.stat().st_mode) == 0o500
    assert stat.S_IMODE(sibling.stat().st_mode) == 0o644


def test_runtime_skips_posix_mode_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    entrypoint = package / "payload" / "worker.exe"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_bytes(b"worker")

    monkeypatch.setattr(plugin_platforms, "_SUPPORTS_EXECUTABLE_MODE", False)

    def unexpected_chmod(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Windows entrypoints must not use POSIX chmod")

    monkeypatch.setattr(plugin_platforms.os, "chmod", unexpected_chmod)

    assert prepare_managed_worker_entrypoint(package, "payload/worker.exe") == entrypoint
