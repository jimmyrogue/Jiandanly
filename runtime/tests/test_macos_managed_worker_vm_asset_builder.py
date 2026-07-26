from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest
import zstandard

ROOT = Path(__file__).parents[2]
BUILDER = ROOT / "client" / "vm-assets" / "build_darwin.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("managed_worker_vm_asset_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vm_asset_builder_extracts_arm64_zboot_by_header() -> None:
    builder = _load_builder()
    image = bytearray(64)
    struct.pack_into("<Q", image, 16, len(image))
    image[56:60] = b"ARMd"
    compressed = zstandard.ZstdCompressor().compress(bytes(image))
    zboot = bytearray(128 + len(compressed) + 16)
    zboot[:2] = b"MZ"
    zboot[4:8] = b"zimg"
    struct.pack_into("<II", zboot, 8, 128, len(compressed))
    zboot[24:28] = b"zstd"
    zboot[128 : 128 + len(compressed)] = compressed

    assert builder.extract_arm64_zboot(bytes(zboot)) == bytes(image)

    struct.pack_into("<I", zboot, 12, len(compressed) + 17)
    with pytest.raises(SystemExit, match="zboot"):
        builder.extract_arm64_zboot(bytes(zboot))


KEY_ID = "6d9f90a6"
FINGERPRINT = "36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6"

# rpm 4 names the short key id; rpm 6 renamed the line, lowercased "signature"
# and swapped in the full fingerprint. Both assert the same thing, and the
# release builds on whichever version homebrew-core happens to serve.
RPM4_OUTPUT = f"""package.rpm:
    Header V4 RSA/SHA256 Signature, key ID {KEY_ID}: OK
    Header SHA256 digest: OK
    Payload SHA256 digest: OK
"""
RPM6_OUTPUT = f"""package.rpm:
    Header OpenPGP V4 RSA/SHA256 signature, key fingerprint: {FINGERPRINT.lower()}: OK
    Header SHA256 digest: OK
    Payload SHA256 digest: OK
"""


@pytest.mark.parametrize("output", [RPM4_OUTPUT, RPM6_OUTPUT])
def test_vm_asset_builder_accepts_either_rpm_signature_spelling(output: str) -> None:
    builder = _load_builder()

    builder.require_rpm_signature(output, key_id=KEY_ID, fingerprint=FINGERPRINT)


@pytest.mark.parametrize(
    "output",
    [
        RPM4_OUTPUT.replace(f"key ID {KEY_ID}: OK", f"key ID {KEY_ID}: NOKEY"),
        RPM6_OUTPUT.replace(f"{FINGERPRINT.lower()}: OK", f"{FINGERPRINT.lower()}: NOKEY"),
        # A signature from a key the lock does not name is not our package.
        RPM4_OUTPUT.replace(KEY_ID, "deadbeef"),
        RPM6_OUTPUT.replace(FINGERPRINT.lower(), "0" * 40),
    ],
)
def test_vm_asset_builder_rejects_an_unexpected_fedora_signature(output: str) -> None:
    builder = _load_builder()

    with pytest.raises(SystemExit, match="not signed by the expected key"):
        builder.require_rpm_signature(output, key_id=KEY_ID, fingerprint=FINGERPRINT)


@pytest.mark.parametrize("digest", ["Header SHA256 digest", "Payload SHA256 digest"])
def test_vm_asset_builder_rejects_a_bad_fedora_digest(digest: str) -> None:
    builder = _load_builder()
    tampered = RPM4_OUTPUT.replace(f"{digest}: OK", f"{digest}: BAD")

    with pytest.raises(SystemExit, match="digest is invalid"):
        builder.require_rpm_signature(tampered, key_id=KEY_ID, fingerprint=FINGERPRINT)
