from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = REPO_ROOT / "runtime" / "plugins" / "ocr" / "runtime-assets"
LOCK_PATH = ASSET_ROOT / "rapidocr-3.9.1.lock.json"


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "ocr_asset_builder", ASSET_ROOT / "build_darwin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rapidocr_asset_lock_freezes_complete_engine_and_models() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["asset_id"] == "org.rapidocr.runtime"
    assert lock["asset_version"] == "3.9.1+ppocrv6-small.1"
    assert lock["python_version"] == "3.12"
    assert lock["platform"] == "darwin/arm64"
    assert lock["build_tools"] == {
        "python": "3.12.10",
        "uv": "0.9.14",
        "pyinstaller": "6.21.0",
    }
    package_names = {item["filename"] for item in lock["packages"]}
    assert "rapidocr-3.9.1-py3-none-any.whl" in package_names
    assert any(name.startswith("onnxruntime-1.27.0-") for name in package_names)
    assert "omegaconf-2.3.1-py3-none-any.whl" in package_names
    assert "antlr4-python3-runtime-4.9.3.tar.gz" in package_names
    assert any(name.startswith("opencv_python_headless-4.12.0.88-") for name in package_names)
    assert not any(name.startswith("opencv_python-5.") for name in package_names)
    assert lock["dependency_policy"]["forbidden_binary_markers"] == [
        "libtesseract",
        "libleptonica",
    ]
    assert {item["filename"] for item in lock["models"]} == {
        "PP-OCRv6_det_small.onnx",
        "PP-OCRv6_rec_small.onnx",
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    }
    for item in [*lock["packages"], *lock["models"]]:
        assert item["size_bytes"] > 0
        assert len(item["sha256"]) == 64


def test_rapidocr_asset_builder_rejects_changed_locked_file(tmp_path: Path) -> None:
    builder = load_builder()
    changed = tmp_path / "changed.whl"
    changed.write_bytes(b"changed")

    with pytest.raises(SystemExit, match="size changed"):
        builder.verify_file(
            changed,
            {"size_bytes": 1, "sha256": "0" * 64},
            "package",
        )
