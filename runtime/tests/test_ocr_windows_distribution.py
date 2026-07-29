from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from shejane_runtime import config

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "runtime" / "plugins" / "ocr"
ASSET_ROOT = OCR_ROOT / "runtime-assets"
LOCK_PATH = ASSET_ROOT / "rapidocr-3.9.1-windows-amd64.lock.json"
BUILDER_PATH = ASSET_ROOT / "build_windows_amd64.py"
FETCHER_PATH = ASSET_ROOT / "fetch_locked_inputs.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("ocr_windows_builder", BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fetcher():
    spec = importlib.util.spec_from_file_location("ocr_windows_fetcher", FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_amd64_lock_freezes_native_rapidocr_dependencies() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["asset_id"] == "org.rapidocr.runtime"
    assert lock["asset_version"] == "3.9.1+ppocrv6-medium.1"
    assert lock["platform"] == "windows/amd64"
    assert lock["build_tools"] == {
        "python": "3.12.10",
        "pyinstaller": "6.21.0",
    }
    package_names = {item["filename"] for item in lock["packages"]}
    assert "pyinstaller-6.21.0-py3-none-win_amd64.whl" in package_names
    assert "pefile-2024.8.26-py3-none-any.whl" in package_names
    assert "pywin32_ctypes-0.2.3-py3-none-any.whl" in package_names
    assert any(name.endswith("-win_amd64.whl") for name in package_names)
    assert not any(
        "macholib" in name or "manylinux" in name or "macosx" in name for name in package_names
    )
    for item in package_names:
        assert item
    for item in lock["packages"]:
        assert item["size_bytes"] > 0
        assert len(item["sha256"]) == 64


def test_windows_builder_rejects_non_amd64_pe(tmp_path: Path) -> None:
    builder = load_builder()
    executable = tmp_path / "ocr-engine.exe"
    payload = bytearray(128)
    payload[:2] = b"MZ"
    payload[0x3C:0x40] = (64).to_bytes(4, "little")
    payload[64:68] = b"PE\0\0"
    payload[68:70] = (0xAA64).to_bytes(2, "little")
    executable.write_bytes(payload)

    with pytest.raises(SystemExit, match="Windows AMD64 PE"):
        builder.verify_amd64_pe(executable)


def test_windows_locked_package_names_resolve_to_canonical_pypi_projects() -> None:
    fetcher = load_fetcher()

    assert fetcher.pypi_project("antlr4-python3-runtime-4.9.3.tar.gz") == "antlr4-python3-runtime"
    assert (
        fetcher.pypi_project("opencv_python_headless-4.12.0.88-cp37-abi3-win_amd64.whl")
        == "opencv-python-headless"
    )
    assert fetcher.pypi_project("pywin32_ctypes-0.2.3-py3-none-any.whl") == "pywin32-ctypes"
    assert fetcher.pypi_version("antlr4-python3-runtime-4.9.3.tar.gz") == "4.9.3"
    assert fetcher.pypi_version("onnxruntime-1.27.0-cp312-cp312-win_amd64.whl") == "1.27.0"


def test_frozen_windows_runtime_discovers_fixed_ocr_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "builtin-plugins" / "ocr-0.1.1-windows-amd64.shejane-plugin"
    asset = (
        tmp_path / "builtin-assets" / "rapidocr-runtime-3.9.1-windows-amd64.shejane-runtime-asset"
    )
    plugin.parent.mkdir()
    asset.parent.mkdir()
    plugin.write_bytes(b"plugin")
    asset.write_bytes(b"asset")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setattr(config.platform, "machine", lambda: "AMD64")

    assert config.default_ocr_package() == plugin
    assert config.default_ocr_runtime_asset() == asset
