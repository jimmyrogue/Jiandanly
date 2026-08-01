#!/usr/bin/env python3
"""Frozen RapidOCR engine used only inside the OCR Runtime Asset."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

from PIL import Image
from rapidocr import (
    EngineType,
    LangCls,
    LangDet,
    LangRec,
    ModelType,
    OCRVersion,
    RapidOCR,
)

ENGINE = {
    "name": "RapidOCR",
    "version": "3.9.1",
    "model": "PP-OCRv6-small",
    "provider": "CPUExecutionProvider",
}
MAX_INPUTS = 16
MAX_IMAGE_PIXELS = 50_000_000
MAX_TOTAL_PIXELS = 160_000_000
MAX_REQUEST_BYTES = 1024 * 1024
IMPORT_TARGET_PATTERNS = (
    re.compile(r"No module named ['\"](?P<module>[A-Za-z0-9_.]+)['\"]"),
    re.compile(
        r"cannot import name ['\"](?P<name>[A-Za-z0-9_]+)['\"] "
        r"from ['\"](?P<module>[A-Za-z0-9_.]+)['\"]"
    ),
    re.compile(r"DLL load failed while importing (?P<module>[A-Za-z0-9_.]+):"),
)


def deny_network(*_args: Any, **_kwargs: Any) -> None:
    raise OSError("network access is disabled for the OCR engine")


def safe_import_target(exc: ImportError) -> str | None:
    imported = getattr(exc, "name", None)
    if isinstance(imported, str) and imported and all(
        part.isidentifier() for part in imported.split(".")
    ):
        return imported
    message = str(exc)
    for pattern in IMPORT_TARGET_PATTERNS:
        if match := pattern.search(message):
            module = match.group("module")
            name = match.groupdict().get("name")
            target = f"{module}.{name}" if name else module
            if len(target) <= 200 and all(
                part.isidentifier() for part in target.split(".")
            ):
                return target
    return None


def asset_payload() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve(strict=True).parents[1]
    override = os.environ.get("SHEJANE_OCR_ASSET_PAYLOAD")
    if not override:
        raise RuntimeError("unfrozen OCR engine requires an explicit test payload")
    return Path(override).resolve(strict=True)


def model_paths() -> dict[str, Path]:
    root = asset_payload() / "models"
    result = {
        "det": root / "PP-OCRv6_det_small.onnx",
        "rec": root / "PP-OCRv6_rec_small.onnx",
        "cls": root / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    }
    for path in result.values():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("locked OCR model is unavailable")
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    return result


def build_engine() -> RapidOCR:
    models = model_paths()
    params: dict[str, Any] = {
        "Global.use_cls": True,
        "Global.log_level": "critical",
        "Global.return_word_box": False,
        "Global.return_single_char_box": False,
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_cann": False,
        "EngineConfig.onnxruntime.use_coreml": False,
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Det.lang_type": LangDet.CH,
        "Det.model_type": ModelType.SMALL,
        "Det.model_path": str(models["det"]),
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Rec.lang_type": LangRec.CH,
        "Rec.model_type": ModelType.SMALL,
        "Rec.model_path": str(models["rec"]),
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Cls.ocr_version": OCRVersion.PPOCRV4,
        "Cls.lang_type": LangCls.CH,
        "Cls.model_type": ModelType.MOBILE,
        "Cls.model_path": str(models["cls"]),
    }
    engine = RapidOCR(params=params)
    sessions = (
        engine.text_det.session.session,
        engine.text_cls.session.session,
        engine.text_rec.session.session,
    )
    if any(session.get_providers() != ["CPUExecutionProvider"] for session in sessions):
        raise RuntimeError("OCR engine activated an undeclared execution provider")
    return engine


def read_request(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
        raise ValueError("invalid OCR request")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("invalid OCR request")
    inputs = raw.get("inputs")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= MAX_INPUTS:
        raise ValueError("invalid OCR request")
    ids: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "path",
            "media_type",
            "size_bytes",
            "sha256",
        }:
            raise ValueError("invalid OCR request")
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in ids:
            raise ValueError("invalid OCR request")
        ids.add(item["id"])
    return inputs


def dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        width, height = image.size
        image.verify()
    if not 1 <= width <= 16_384 or not 1 <= height <= 16_384:
        raise ValueError("image dimensions exceed the supported limit")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("image dimensions exceed the supported limit")
    return width, height


def recognize(engine: RapidOCR, inputs: list[dict[str, Any]]) -> dict[str, Any]:
    images: list[dict[str, Any]] = []
    total_pixels = 0
    for item in inputs:
        path = Path(item["path"])
        if path.is_symlink() or not path.is_file():
            raise ValueError("OCR input is unavailable")
        width, height = dimensions(path)
        total_pixels += width * height
        if total_pixels > MAX_TOTAL_PIXELS:
            raise ValueError("image dimensions exceed the supported limit")
        result = engine(path)
        boxes = getattr(result, "boxes", None)
        texts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        lines: list[dict[str, Any]] = []
        if boxes is not None or texts is not None or scores is not None:
            if boxes is None or texts is None or scores is None:
                raise RuntimeError("RapidOCR returned an incomplete result")
            if not len(boxes) == len(texts) == len(scores):
                raise RuntimeError("RapidOCR returned inconsistent result lengths")
            for box, text, score in zip(boxes, texts, scores, strict=True):
                lines.append(
                    {
                        "text": str(text),
                        "confidence": float(score),
                        "polygon": [[float(value) for value in point] for point in box],
                    }
                )
        images.append(
            {
                "input_id": item["id"],
                "width": width,
                "height": height,
                "lines": lines,
            }
        )
    return {"engine": ENGINE, "images": images}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(2)
    request_path = Path(sys.argv[1]).resolve(strict=True)
    response_path = Path(sys.argv[2]).resolve(strict=False)
    inputs = read_request(request_path)
    socket.create_connection = deny_network  # type: ignore[assignment]
    socket.socket.connect = deny_network  # type: ignore[method-assign]
    result = recognize(build_engine(), inputs)
    temporary = response_path.with_suffix(response_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(response_path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        diagnostic = type(exc).__name__
        if isinstance(exc, ImportError) and (imported := safe_import_target(exc)):
            diagnostic = f"{diagnostic}|{imported}"
        print(f"OCR engine failed: {diagnostic}", file=sys.stderr)
        raise SystemExit(2) from None
