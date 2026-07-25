#!/usr/bin/env python3
"""Build one signed, architecture-specific macOS Managed Worker VM asset set."""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import zstandard

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME_ROOT / "src"))

from shejane_runtime.plugins.guest_image import build_linux_initramfs  # noqa: E402

LOCK_PATH = Path(__file__).with_name("darwin-arm64.lock.json")
GUESTD_SOURCE = RUNTIME_ROOT / "src" / "shejane_runtime" / "plugins" / "guestd" / "main.go"
LAUNCHER_BUILD = ROOT / "scripts" / "build-macos-managed-worker-vm.sh"
_ARM64_MACH_CPU = 0x0100000C
_MAX_KERNEL_BYTES = 256 * 1024 * 1024


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel-core", type=Path, required=True)
    parser.add_argument("--kernel-modules-core", type=Path, required=True)
    parser.add_argument("--kernel-srpm", type=Path, required=True)
    parser.add_argument("--fedora-keyring", type=Path, required=True)
    parser.add_argument("--e2fs-source", type=Path, required=True)
    parser.add_argument("--e2fs-signature", type=Path, required=True)
    parser.add_argument("--e2fs-signing-key", type=Path, required=True)
    parser.add_argument("--guest-rootfs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codesign-identity", default="-")
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine().lower() != "arm64":
        parser.error("this locked asset builder requires native macOS arm64")
    if args.output.exists() or args.output.is_symlink():
        parser.error("--output must not already exist")
    args.output = args.output.resolve(strict=False)

    lock = load_lock()
    inputs = {
        "kernel_core": verify_locked_file(args.kernel_core, lock["fedora"]["kernel_core"]),
        "kernel_modules_core": verify_locked_file(
            args.kernel_modules_core, lock["fedora"]["kernel_modules_core"]
        ),
        "kernel_srpm": verify_locked_file(args.kernel_srpm, lock["fedora"]["kernel_srpm"]),
        "fedora_keyring": verify_locked_file(args.fedora_keyring, lock["fedora"]["keyring"]),
        "e2fs_source": verify_locked_file(args.e2fs_source, lock["e2fsprogs"]["source"]),
        "e2fs_signature": verify_locked_file(args.e2fs_signature, lock["e2fsprogs"]["signature"]),
        "e2fs_signing_key": verify_locked_file(
            args.e2fs_signing_key, lock["e2fsprogs"]["signing_key"]
        ),
        "guest_rootfs": verify_locked_file(args.guest_rootfs, lock["rootfs"]["image"]),
    }
    toolchain = verify_toolchain(lock)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="shejane-managed-worker-vm-assets-", dir=args.output.parent
    ) as temporary:
        work = Path(temporary)
        stage = work / "asset-set"
        stage.mkdir()
        verify_fedora_rpms(inputs, lock, work)
        e2fs_tar = verify_e2fs_source(inputs, lock, work)
        build_guest(stage, inputs, lock, work)
        build_mke2fs(stage, e2fs_tar, lock, toolchain, work, max(1, args.jobs))
        build_launcher(stage, args.codesign_identity)
        sign_and_verify(stage / "mke2fs", args.codesign_identity)
        write_metadata(stage, inputs, lock, toolchain)
        set_permissions(stage)
        os.replace(stage, args.output)
    print(args.output.resolve())


def load_lock() -> dict[str, Any]:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if (
        lock.get("schema_version") != 1
        or lock.get("target") != "darwin/arm64"
        or lock.get("protocol_version") != 1
        or lock.get("fedora", {}).get("kernel_nvr") != "6.19.10-300.fc44"
        or lock.get("e2fsprogs", {}).get("version") != "1.47.2"
    ):
        raise SystemExit("unsupported Managed Worker VM asset lock")
    return lock


def verify_locked_file(path: Path, expected: dict[str, Any]) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"locked input is not a regular file: {path}")
    path = path.resolve(strict=True)
    size, digest = file_identity(path)
    if size != int(expected["size"]) or digest != str(expected["sha256"]):
        raise SystemExit(f"locked input identity changed: {path.name}")
    return path


def file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def verify_fedora_rpms(inputs: dict[str, Path], lock: dict[str, Any], work: Path) -> None:
    fedora = lock["fedora"]
    exported_key = work / "fedora-release-key.asc"
    expected_fingerprint = str(fedora["signing_key_fingerprint"])
    with tempfile.TemporaryDirectory(prefix="sj-fedora-gpg-") as temporary_home:
        home = Path(temporary_home)
        run(
            [
                "gpg",
                "--homedir",
                str(home),
                "--batch",
                "--no-autostart",
                "--import",
                str(inputs["fedora_keyring"]),
            ]
        )
        fingerprints = run(
            [
                "gpg",
                "--homedir",
                str(home),
                "--batch",
                "--no-autostart",
                "--with-colons",
                "--fingerprint",
            ],
            capture=True,
        ).stdout.splitlines()
        if expected_fingerprint not in {
            line.split(":")[9] for line in fingerprints if line.startswith("fpr:")
        }:
            raise SystemExit("Fedora signing key fingerprint changed")
        exported_key.write_text(
            run(
                [
                    "gpg",
                    "--homedir",
                    str(home),
                    "--batch",
                    "--no-autostart",
                    "--armor",
                    "--export",
                    expected_fingerprint,
                ],
                capture=True,
            ).stdout,
            encoding="ascii",
        )
    rpmdb = work / "rpmdb"
    rpmdb.mkdir()
    run(["rpmkeys", "--dbpath", str(rpmdb), "--import", str(exported_key)])
    for name in ("kernel_core", "kernel_modules_core", "kernel_srpm"):
        output = run(
            [
                "rpmkeys",
                "--dbpath",
                str(rpmdb),
                "--checksig",
                "--verbose",
                str(inputs[name]),
            ],
            capture=True,
        )
        require_rpm_signature(
            output.stdout + output.stderr,
            key_id=str(fedora["signing_key_id"]),
        )
        identity = run(
            [
                "rpm",
                "--dbpath",
                str(rpmdb),
                "-qp",
                "--queryformat",
                "%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{SOURCERPM}",
                str(inputs[name]),
            ],
            capture=True,
        ).stdout
        if identity != fedora[name]["rpm_identity"]:
            raise SystemExit(f"Fedora RPM identity changed: {name}")


def require_rpm_signature(output: str, *, key_id: str) -> None:
    required = (
        f"Header V4 RSA/SHA256 Signature, key ID {key_id}: OK",
        "Header SHA256 digest: OK",
        "Payload SHA256 digest: OK",
    )
    if any(item not in output for item in required):
        raise SystemExit("Fedora RPM signature or digest is invalid")


def verify_e2fs_source(inputs: dict[str, Path], lock: dict[str, Any], work: Path) -> Path:
    expected = lock["e2fsprogs"]["signing_key"]
    uncompressed = work / "e2fsprogs-1.47.2.tar"
    with tempfile.TemporaryDirectory(prefix="sj-e2fs-gpg-") as temporary_home:
        home = Path(temporary_home)
        run(
            [
                "gpg",
                "--homedir",
                str(home),
                "--batch",
                "--no-autostart",
                "--import",
                str(inputs["e2fs_signing_key"]),
            ]
        )
        fingerprints = run(
            [
                "gpg",
                "--homedir",
                str(home),
                "--batch",
                "--no-autostart",
                "--with-colons",
                "--fingerprint",
                "--fingerprint",
            ],
            capture=True,
        ).stdout.splitlines()
        actual = {line.split(":")[9] for line in fingerprints if line.startswith("fpr:")}
        if not {
            expected["primary_fingerprint"],
            expected["signing_fingerprint"],
        }.issubset(actual):
            raise SystemExit("e2fsprogs signing key fingerprint changed")

        with lzma.open(inputs["e2fs_source"], "rb") as source, uncompressed.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        verification = run(
            [
                "gpg",
                "--homedir",
                str(home),
                "--batch",
                "--no-autostart",
                "--status-fd",
                "1",
                "--verify",
                str(inputs["e2fs_signature"]),
                str(uncompressed),
            ],
            capture=True,
        ).stdout
        if f"[GNUPG:] VALIDSIG {expected['signing_fingerprint']} " not in verification:
            raise SystemExit("e2fsprogs source signature is invalid")
    return uncompressed


def build_guest(stage: Path, inputs: dict[str, Path], lock: dict[str, Any], work: Path) -> None:
    fedora = lock["fedora"]
    vmlinuz = rpm_member(inputs["kernel_core"], fedora["kernel_core"]["vmlinuz_member"])
    image = extract_arm64_zboot(vmlinuz)
    expected_image = fedora["linux_image"]
    if (
        len(image) != expected_image["size"]
        or hashlib.sha256(image).hexdigest() != expected_image["sha256"]
    ):
        raise SystemExit("decompressed Fedora Linux Image changed")
    (stage / "linux-kernel").write_bytes(image)

    module_paths = []
    modules = work / "modules"
    modules.mkdir()
    for name, member in fedora["kernel_modules_core"]["module_members"].items():
        path = modules / name
        try:
            path.write_bytes(lzma.decompress(rpm_member(inputs["kernel_modules_core"], member)))
        except lzma.LZMAError as exc:
            raise SystemExit(f"Fedora VSOCK module compression changed: {name}") from exc
        module_paths.append(path)
    build_linux_initramfs(
        GUESTD_SOURCE,
        stage / "initramfs.cpio",
        architecture="arm64",
        module_paths=tuple(module_paths),
    )
    shutil.copyfile(inputs["guest_rootfs"], stage / "guest-rootfs.ext4")


def extract_arm64_zboot(vmlinuz: bytes) -> bytes:
    if (
        len(vmlinuz) < 64
        or vmlinuz[:2] != b"MZ"
        or vmlinuz[4:8] != b"zimg"
        or vmlinuz[24:28] != b"zstd"
    ):
        raise SystemExit("Fedora arm64 zboot header changed")
    offset, size = struct.unpack_from("<II", vmlinuz, 8)
    if offset < 64 or size < 1 or offset + size > len(vmlinuz):
        raise SystemExit("Fedora arm64 zboot payload is invalid")
    compressed = vmlinuz[offset : offset + size]
    zstd = shutil.which("zstd")
    if zstd is None:
        raise SystemExit("zstd is required to unpack the Fedora arm64 kernel")
    image = subprocess.run(
        [zstd, "-d", "-q", "-c"],
        input=compressed,
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    if (
        len(image) < 64
        or len(image) > _MAX_KERNEL_BYTES
        or image[56:60] != b"ARMd"
        or struct.unpack_from("<Q", image, 16)[0] != len(image)
    ):
        raise SystemExit("decompressed Fedora arm64 Image is invalid")
    return image


def rpm_member(rpm: Path, member: str) -> bytes:
    if not member.startswith("./") or ".." in Path(member).parts:
        raise SystemExit("locked RPM member path is invalid")
    return run(["/usr/bin/tar", "-xOf", str(rpm), member], binary=True).stdout


def build_mke2fs(
    stage: Path,
    source_tar: Path,
    lock: dict[str, Any],
    toolchain: dict[str, str],
    work: Path,
    jobs: int,
) -> None:
    source_root = work / "e2fs-source"
    source_root.mkdir()
    with tarfile.open(source_tar, "r:") as archive:
        archive.extractall(source_root, filter="data")
    roots = [path for path in source_root.iterdir() if path.is_dir()]
    if len(roots) != 1 or roots[0].name != "e2fsprogs-1.47.2":
        raise SystemExit("e2fsprogs source archive layout changed")
    source = roots[0]
    build = work / "e2fs-build"
    build.mkdir()
    expected = lock["e2fsprogs"]
    flags = " ".join(
        (
            "-O2 -g0",
            f"-ffile-prefix-map={source}=/usr/src/e2fsprogs",
            f"-fdebug-prefix-map={source}=/usr/src/e2fsprogs",
            f"-fmacro-prefix-map={source}=/usr/src/e2fsprogs",
            f"-ffile-prefix-map={build}=/usr/src/e2fsprogs-build",
            f"-fdebug-prefix-map={build}=/usr/src/e2fsprogs-build",
            f"-fmacro-prefix-map={build}=/usr/src/e2fsprogs-build",
        )
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "CC": "/usr/bin/clang",
        "CFLAGS": flags,
        "CPPFLAGS": "",
        "LDFLAGS": "",
        "PKG_CONFIG": "/usr/bin/false",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "SOURCE_DATE_EPOCH": str(expected["source_date_epoch"]),
        "ZERO_AR_DATE": "1",
        "SDKROOT": toolchain["sdk_path"],
        "MACOSX_DEPLOYMENT_TARGET": "11.0",
    }
    run([str(source / "configure"), *expected["configure"]], cwd=build, env=environment)
    run(["/usr/bin/make", f"-j{jobs}", "libs"], cwd=build, env=environment)
    run(["/usr/bin/make", f"-j{jobs}", "-C", "misc", "mke2fs"], cwd=build, env=environment)
    binary = build / "misc" / "mke2fs"
    if file_identity(binary)[1] != expected["unsigned_mke2fs_sha256"]:
        raise SystemExit("mke2fs reproducible build digest changed")
    version_result = run([str(binary), "-V"], capture=True)
    version = version_result.stdout + version_result.stderr
    contents = binary.read_bytes()
    if (
        "mke2fs 1.47.2" not in version
        or len(contents) < 8
        or contents[:4] != b"\xcf\xfa\xed\xfe"
        or struct.unpack_from("<I", contents, 4)[0] != _ARM64_MACH_CPU
    ):
        raise SystemExit("built mke2fs identity changed")
    linked = run(["/usr/bin/otool", "-L", str(binary)], capture=True).stdout.splitlines()[1:]
    if [line.strip().split(" ", 1)[0] for line in linked] != ["/usr/lib/libSystem.B.dylib"]:
        raise SystemExit("built mke2fs links an undeclared library")
    build_version = run(["/usr/bin/otool", "-l", str(binary)], capture=True).stdout
    if "minos 11.0" not in build_version or f"sdk {toolchain['sdk_version']}" not in build_version:
        raise SystemExit("built mke2fs deployment target changed")
    shutil.copy2(binary, stage / "mke2fs")


def verify_toolchain(lock: dict[str, Any]) -> dict[str, str]:
    """Pin the tools whose versions can change the produced assets.

    rpm is deliberately absent. It never touches an output byte: the kernel
    payloads are unpacked with bsdtar, and rpm/rpmkeys only verify signatures
    and query identity of inputs this lock already pins by URL and digest.
    Locking its version made every unrelated homebrew-core bump fail a release
    while adding no reproducibility -- require_rpm_signature() and the identity
    comparison are what actually guard those inputs.
    """

    expected = lock["toolchain"]
    xcode = run(["/usr/bin/xcodebuild", "-version"], capture=True).stdout.splitlines()
    actual = {
        "bsdtar_version": run(["/usr/bin/tar", "--version"], capture=True).stdout.splitlines()[0],
        "gpg_version": run(["gpg", "--version"], capture=True).stdout.splitlines()[0],
        "go_version": run(["go", "version"], capture=True).stdout.split()[2],
        "xcode_version": xcode[0].removeprefix("Xcode "),
        "xcode_build": xcode[1].removeprefix("Build version "),
        "clang_version": run(["/usr/bin/clang", "--version"], capture=True).stdout.splitlines()[0],
        "sdk_version": run(
            ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"], capture=True
        ).stdout.strip(),
        "make_version": run(["/usr/bin/make", "--version"], capture=True).stdout.splitlines()[0],
        "python_version": platform.python_version(),
        "zstandard_python_version": zstandard.__version__,
        "zstd_version": run(["zstd", "--version"], capture=True).stdout.strip(),
    }
    if actual != expected:
        raise SystemExit(f"Darwin VM asset toolchain changed: {actual!r}")
    return actual | {
        "sdk_path": run(
            ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], capture=True
        ).stdout.strip()
    }


def build_launcher(stage: Path, identity: str) -> None:
    environment = os.environ.copy()
    environment["SHEJANE_CODESIGN_IDENTITY"] = identity
    run([str(LAUNCHER_BUILD), str(stage / "shejane-managed-worker-vm")], env=environment)


def sign_and_verify(path: Path, identity: str) -> None:
    options = ["--options", "runtime", "--timestamp"] if identity != "-" else []
    run(["/usr/bin/codesign", "--force", "--sign", identity, *options, str(path)])
    run(["/usr/bin/codesign", "--verify", "--strict", str(path)])


def write_metadata(
    stage: Path,
    inputs: dict[str, Path],
    lock: dict[str, Any],
    toolchain: dict[str, str],
) -> None:
    licenses = stage / "licenses"
    licenses.mkdir()
    fedora = lock["fedora"]
    (licenses / "linux-kernel.txt").write_bytes(
        rpm_member(inputs["kernel_core"], fedora["kernel_core"]["license_member"])
    )
    with tarfile.open(inputs["e2fs_source"], "r:xz") as archive:
        for source_name, destination in (
            ("e2fsprogs-1.47.2/NOTICE", "e2fsprogs-NOTICE.txt"),
            ("e2fsprogs-1.47.2/lib/uuid/COPYING", "e2fsprogs-libuuid-COPYING.txt"),
        ):
            member = archive.getmember(source_name)
            extracted = archive.extractfile(member)
            if extracted is None or not member.isfile() or member.size > 1024 * 1024:
                raise SystemExit("e2fsprogs license source changed")
            (licenses / destination).write_bytes(extracted.read())

    public_toolchain = {key: value for key, value in toolchain.items() if key != "sdk_path"}
    lock_digest = hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest()
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "shejane-managed-worker-vm-darwin-arm64",
        "documentNamespace": f"https://shejane.org/spdx/managed-worker-vm/{lock_digest}",
        "creationInfo": {
            "created": lock["created_at"],
            "creators": ["Organization: SheJane"],
        },
        "packages": [
            spdx_package(
                "Fedora kernel",
                "SPDXRef-Package-Fedora-Kernel",
                fedora["kernel_nvr"],
                fedora["kernel_srpm"]["url"],
                fedora["kernel_srpm"]["sha256"],
                "GPL-2.0-only",
            ),
            spdx_package(
                "e2fsprogs mke2fs",
                "SPDXRef-Package-E2fsprogs",
                lock["e2fsprogs"]["version"],
                lock["e2fsprogs"]["source"]["url"],
                lock["e2fsprogs"]["source"]["sha256"],
                "GPL-2.0-only AND LGPL-2.0-only AND BSD-3-Clause AND MIT",
            ),
            spdx_package(
                "Debian Managed Worker guest rootfs",
                "SPDXRef-Package-Debian-Rootfs",
                lock["rootfs"]["debian_version"],
                lock["rootfs"]["source"]["url"],
                lock["rootfs"]["source"]["sha256"],
                "NOASSERTION",
            ),
            {
                "name": "SheJane Managed Worker guest and launcher",
                "SPDXID": "SPDXRef-Package-SheJane-Managed-Worker",
                "versionInfo": lock_digest[:16],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "AGPL-3.0-only",
                "licenseDeclared": "AGPL-3.0-only",
                "copyrightText": "NOASSERTION",
            },
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": item,
            }
            for item in (
                "SPDXRef-Package-Fedora-Kernel",
                "SPDXRef-Package-E2fsprogs",
                "SPDXRef-Package-Debian-Rootfs",
                "SPDXRef-Package-SheJane-Managed-Worker",
            )
        ],
    }
    (stage / "sbom.spdx.json").write_text(
        json.dumps(sbom, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    source_signature = f"openpgp:{fedora['signing_key_fingerprint']}"
    sources = [
        source_record(
            "fedora-kernel-core", fedora["kernel_nvr"], fedora["kernel_core"], source_signature
        ),
        source_record(
            "fedora-kernel-modules-core",
            fedora["kernel_nvr"],
            fedora["kernel_modules_core"],
            source_signature,
        ),
        source_record(
            "fedora-kernel-srpm", fedora["kernel_nvr"], fedora["kernel_srpm"], source_signature
        ),
        source_record(
            "e2fsprogs",
            lock["e2fsprogs"]["version"],
            lock["e2fsprogs"]["source"],
            (
                f"openpgp:{lock['e2fsprogs']['signing_key']['signing_fingerprint']};"
                f"url={lock['e2fsprogs']['signature']['url']};"
                f"sha256={lock['e2fsprogs']['signature']['sha256']}"
            ),
        ),
        source_record(
            "debian-bookworm-slim-arm64",
            lock["rootfs"]["debian_version"],
            lock["rootfs"]["source"],
            (
                f"oci-manifest:sha256:{lock['rootfs']['source']['sha256']};"
                f"revision={lock['rootfs']['source_revision']}"
            ),
        ),
    ]
    files = {
        "kernel": file_record(stage, "linux-kernel"),
        "initramfs": file_record(stage, "initramfs.cpio"),
        "rootfs": file_record(stage, "guest-rootfs.ext4"),
        "mke2fs": file_record(stage, "mke2fs"),
        "launcher": file_record(stage, "shejane-managed-worker-vm"),
    }
    payload = {
        "schema_version": 1,
        "host": {"os": "darwin", "arch": "arm64"},
        "guest": {"os": "linux", "arch": "arm64"},
        "protocol_version": 1,
        "files": files,
        "sources": sources,
        "build": {
            "builder_version": "1",
            "lock_sha256": lock_digest,
            "guestd_sha256": file_identity(GUESTD_SOURCE)[1],
            "launcher_source_sha256": file_identity(
                ROOT / "client/native/managed-worker-vm.swift"
            )[1],
            "kernel_nvr": fedora["kernel_nvr"],
            "rootfs_oci_manifest": lock["rootfs"]["source"]["sha256"],
            "rootfs_package_manifest": lock["rootfs"]["package_manifest_sha256"],
            "e2fsprogs_configure": " ".join(lock["e2fsprogs"]["configure"]),
            **public_toolchain,
        },
        "sbom": file_record(stage, "sbom.spdx.json"),
        "licenses": [
            file_record(stage, "licenses/linux-kernel.txt"),
            file_record(stage, "licenses/e2fsprogs-NOTICE.txt"),
            file_record(stage, "licenses/e2fsprogs-libuuid-COPYING.txt"),
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["asset_set_id"] = (
        "darwin-arm64/sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    )
    (stage / "manifest.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def source_record(
    name: str, version: str, source: dict[str, Any], signature: str
) -> dict[str, str]:
    return {
        "name": name,
        "version": version,
        "url": source["url"],
        "sha256": "sha256:" + source["sha256"],
        "signature": signature,
    }


def spdx_package(
    name: str, spdx_id: str, version: str, url: str, digest: str, license_id: str
) -> dict[str, Any]:
    return {
        "name": name,
        "SPDXID": spdx_id,
        "versionInfo": version,
        "downloadLocation": url,
        "filesAnalyzed": False,
        "licenseConcluded": license_id,
        "licenseDeclared": license_id,
        "copyrightText": "NOASSERTION",
        "checksums": [{"algorithm": "SHA256", "checksumValue": digest}],
    }


def file_record(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    size, digest = file_identity(path)
    return {"path": relative, "size": size, "sha256": "sha256:" + digest}


def set_permissions(root: Path) -> None:
    executables = {root / "mke2fs", root / "shejane-managed-worker-vm"}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SystemExit("VM asset builder produced a symlink")
        path.chmod(0o555 if path in executables or path.is_dir() else 0o444)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    binary: bool = False,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=capture or binary,
        text=not binary,
        encoding=None if binary else "utf-8",
        timeout=300,
    )


if __name__ == "__main__":
    main()
