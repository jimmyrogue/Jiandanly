#!/usr/bin/env python3
"""One-shot OCR Managed Worker using an exact RapidOCR Runtime Asset."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

ASSET_ID = "org.rapidocr.runtime"
ENGINE_IDENTITY = {
    "name": "RapidOCR",
    "version": "3.9.1",
    "model": "PP-OCRv6-small",
    "provider": "CPUExecutionProvider",
}
SUPPORTED_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/tiff",
    "image/bmp",
}
MAX_INPUTS = 16
MAX_IMAGE_PIXELS = 50_000_000
MAX_TOTAL_PIXELS = 160_000_000
MAX_ENGINE_LINES = 10_000
MAX_ENGINE_RESPONSE_BYTES = 16 * 1024 * 1024


class OcrActionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def receive() -> dict[str, Any]:
    return json.loads(sys.stdin.buffer.readline())


def send(value: dict[str, Any]) -> None:
    frame = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


def response(request_id: int, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


class Progress:
    def __init__(self, invocation: dict[str, Any]) -> None:
        self.invocation = invocation
        self.sequence = 0

    def emit(
        self,
        phase: str,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
        unit: str | None = None,
    ) -> None:
        self.sequence += 1
        params: dict[str, Any] = {
            "schema_version": 1,
            "invocation_id": self.invocation["invocation_id"],
            "operation_id": self.invocation["operation_id"],
            "sequence": self.sequence,
            "phase": phase,
            "message": message,
        }
        if completed is not None:
            params["completed"] = completed
        if total is not None:
            params["total"] = total
        if unit is not None:
            params["unit"] = unit
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})


def contained_file(root: Path, virtual_path: str) -> Path:
    try:
        relative = PurePosixPath(virtual_path).relative_to("/input")
    except ValueError as exc:
        raise OcrActionError("invalid_input", "OCR input path is invalid") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OcrActionError("invalid_input", "OCR input path is invalid")
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise OcrActionError("invalid_input", "OCR input is unavailable")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise OcrActionError("invalid_input", "OCR input is unavailable") from exc
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_inputs(invocation: dict[str, Any]) -> list[dict[str, Any]]:
    input_ids = invocation["arguments"]["input_ids"]
    if (
        not isinstance(input_ids, list)
        or not 1 <= len(input_ids) <= MAX_INPUTS
        or any(not isinstance(value, str) or not value for value in input_ids)
        or len(set(input_ids)) != len(input_ids)
    ):
        raise OcrActionError("invalid_input", "OCR input selection is invalid")
    available = {item["id"]: item for item in invocation["inputs"]}
    selected: list[dict[str, Any]] = []
    input_root = Path(os.environ["SHEJANE_PLUGIN_INPUT_ROOT"])
    for input_id in input_ids:
        reference = available.get(input_id)
        if not isinstance(reference, dict):
            raise OcrActionError("invalid_input", "selected OCR input is unavailable")
        if reference.get("media_type") not in SUPPORTED_MEDIA_TYPES:
            raise OcrActionError("invalid_input", "selected input is not a supported image")
        source = contained_file(input_root, str(reference["path"]))
        size = source.stat().st_size
        digest = sha256_file(source)
        if size != reference.get("size_bytes") or digest != reference.get("sha256"):
            raise OcrActionError("invalid_input", "selected OCR input identity changed")
        selected.append(
            {
                "id": input_id,
                "path": str(source),
                "media_type": reference["media_type"],
                "size_bytes": size,
                "sha256": digest,
            }
        )
    return selected


def output_root() -> Path:
    return Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"]).resolve(strict=True)


def output_file(name: str) -> Path:
    root = output_root()
    destination = root / name
    destination.parent.resolve(strict=True).relative_to(root)
    return destination


def runtime_temp() -> Path:
    temporary = output_root() / ".runtime-tmp"
    temporary.mkdir(mode=0o700, exist_ok=True)
    temporary.resolve(strict=True).relative_to(output_root())
    return temporary


def engine_binary() -> Path:
    try:
        assets = json.loads(os.environ["SHEJANE_PLUGIN_RUNTIME_ASSETS"])
        payload = Path(assets[ASSET_ID]).resolve(strict=True)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise OcrActionError(
            "runtime_unavailable", "RapidOCR Runtime Asset is unavailable"
        ) from exc
    executable = payload / "bin" / ("ocr-engine.exe" if os.name == "nt" else "ocr-engine")
    if executable.is_symlink() or not executable.is_file():
        raise OcrActionError("runtime_unavailable", "OCR engine is unavailable")
    try:
        executable.resolve(strict=True).relative_to(payload)
    except (OSError, ValueError) as exc:
        raise OcrActionError("runtime_unavailable", "OCR engine is unavailable") from exc
    return executable


def engine_environment(temporary: Path) -> dict[str, str]:
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SystemRoot", "WINDIR"}
    }
    return allowed | {
        "HOME": str(temporary),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
    }


def safe_engine_error_type(raw: bytes) -> str | None:
    prefix = "OCR engine failed: "
    for line in reversed(raw.decode("ascii", errors="ignore").splitlines()):
        diagnostic = line.strip()
        if diagnostic.startswith(prefix):
            parts = diagnostic.removeprefix(prefix).split("|", 1)
            error_type = parts[0]
            imported = parts[1] if len(parts) == 2 else None
        elif ": " in diagnostic:
            error_type = diagnostic.split(": ", 1)[0]
            imported = None
        else:
            continue
        if (
            error_type.isidentifier()
            and len(error_type) <= 100
            and error_type.endswith(("Error", "Exception"))
        ):
            if imported and len(imported) <= 200 and all(
                part.isidentifier() for part in imported.split(".")
            ):
                return f"{error_type}: {imported}"
            return error_type
    return None


def run_engine(command: list[str], temporary: Path) -> None:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=engine_environment(temporary),
    )
    assert process.stdout is not None and process.stderr is not None
    overflow: list[str] = []
    stderr = bytearray()

    def drain(name: str, stream: Any, limit: int) -> None:
        size = 0
        while data := stream.read(64 * 1024):
            size += len(data)
            if size > limit:
                overflow.append(name)
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return
            if name == "stderr":
                stderr.extend(data)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout, 64 * 1024)),
        threading.Thread(target=drain, args=("stderr", process.stderr, 128 * 1024)),
    ]
    for thread in threads:
        thread.start()
    returncode = process.wait()
    for thread in threads:
        thread.join()
    if overflow:
        raise OcrActionError("resource_exhausted", f"OCR engine {overflow[0]} exceeded its limit")
    if returncode != 0:
        message = "OCR engine could not process the selected images"
        if error_type := safe_engine_error_type(stderr):
            message = f"{message} ({error_type})"
        elif returncode:
            message = (
                f"OCR engine exited 0x{returncode & 0xFFFFFFFF:08X}; "
                "it could not process the selected images"
            )
        raise OcrActionError("ocr_failed", message)


def finite_number(value: Any, *, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
    return number


def positive_int(value: Any, *, maximum: int) -> int:
    number = finite_number(value, minimum=1, maximum=maximum)
    if not number.is_integer():
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
    return int(number)


def normalized_text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 10_000:
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
    without_controls = "".join(
        " "
        if character in "\t\n\r"
        else ""
        if unicodedata.category(character) == "Cc"
        else character
        for character in value
    )
    return " ".join(without_controls.split())


def normalized_polygon(value: Any, width: int, height: int) -> list[list[int]]:
    if not isinstance(value, list) or len(value) != 4:
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
    result: list[list[int]] = []
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
        x = finite_number(point[0], minimum=-1_000_000, maximum=1_000_000)
        y = finite_number(point[1], minimum=-1_000_000, maximum=1_000_000)
        result.append(
            [
                min(width - 1, max(0, round(x))),
                min(height - 1, max(0, round(y))),
            ]
        )
    return result


def load_engine_response(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ENGINE_RESPONSE_BYTES:
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid") from exc
    if not isinstance(raw, dict):
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
    return raw


def normalize_response(
    raw: dict[str, Any], selected: list[dict[str, Any]], arguments: dict[str, Any]
) -> dict[str, Any]:
    if raw.get("engine") != ENGINE_IDENTITY:
        raise OcrActionError("engine_protocol_violation", "OCR engine identity is invalid")
    raw_images = raw.get("images")
    if not isinstance(raw_images, list) or len(raw_images) != len(selected):
        raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")

    minimum_confidence = finite_number(
        arguments["minimum_confidence"], minimum=0, maximum=1
    )
    remaining_lines = positive_int(arguments["max_lines"], maximum=10_000)
    remaining_characters = positive_int(arguments["max_characters"], maximum=200_000)
    total_pixels = 0
    normalized_images: list[dict[str, Any]] = []

    for expected, raw_image in zip(selected, raw_images, strict=True):
        if not isinstance(raw_image, dict) or raw_image.get("input_id") != expected["id"]:
            raise OcrActionError("engine_protocol_violation", "OCR engine image order is invalid")
        width = positive_int(raw_image.get("width"), maximum=16_384)
        height = positive_int(raw_image.get("height"), maximum=16_384)
        pixels = width * height
        total_pixels += pixels
        if pixels > MAX_IMAGE_PIXELS or total_pixels > MAX_TOTAL_PIXELS:
            raise OcrActionError("resource_exhausted", "OCR image dimensions exceed the supported limit")
        raw_lines = raw_image.get("lines")
        if not isinstance(raw_lines, list) or len(raw_lines) > MAX_ENGINE_LINES:
            raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")

        lines: list[dict[str, Any]] = []
        truncated = False
        for raw_line in raw_lines:
            if not isinstance(raw_line, dict):
                raise OcrActionError("engine_protocol_violation", "OCR engine response is invalid")
            text = normalized_text(raw_line.get("text"))
            confidence = finite_number(raw_line.get("confidence"), minimum=0, maximum=1)
            polygon = normalized_polygon(raw_line.get("polygon"), width, height)
            if not text or confidence < minimum_confidence:
                continue
            if remaining_lines == 0 or remaining_characters == 0:
                truncated = True
                continue
            kept_text = text[:remaining_characters]
            if len(kept_text) < len(text):
                truncated = True
            if kept_text:
                lines.append(
                    {
                        "text": kept_text,
                        "confidence": round(confidence, 5),
                        "polygon": polygon,
                    }
                )
                remaining_lines -= 1
                remaining_characters -= len(kept_text)
        full_text = "\n".join(line["text"] for line in lines)
        normalized_images.append(
            {
                "input_id": expected["id"],
                "width": width,
                "height": height,
                "lines": lines,
                "full_text": full_text,
                "truncated": truncated,
            }
        )

    total_lines = sum(len(image["lines"]) for image in normalized_images)
    total_characters = sum(
        len(line["text"])
        for image in normalized_images
        for line in image["lines"]
    )
    return {
        "engine": dict(ENGINE_IDENTITY),
        "images": normalized_images,
        "total_lines": total_lines,
        "total_characters": total_characters,
    }


def recognize_images(
    invocation: dict[str, Any], progress: Progress
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    selected = selected_inputs(invocation)
    arguments = invocation["arguments"]
    temporary = runtime_temp()
    request_path = temporary / "request.json"
    response_path = temporary / "response.json"
    request_path.write_text(
        json.dumps(
            {"schema_version": 1, "inputs": selected},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    progress.emit(
        "recognize.images",
        "Recognizing text in images",
        completed=0,
        total=len(selected),
        unit="images",
    )
    run_engine([str(engine_binary()), str(request_path), str(response_path)], temporary)
    output = normalize_response(load_engine_response(response_path), selected, arguments)

    artifacts: list[dict[str, str]] = []
    text_artifact_name: str | None = None
    json_artifact_name: str | None = None
    if bool(arguments["include_text_artifact"]):
        text_artifact_name = "ocr.txt"
        body = "\n\n".join(
            f"--- {image['input_id']} ---\n{image['full_text']}" for image in output["images"]
        )
        output_file(text_artifact_name).write_text(body + "\n", encoding="utf-8")
        artifacts.append(
            {
                "path": f"/output/{text_artifact_name}",
                "media_type": "text/plain",
                "name": text_artifact_name,
            }
        )
    if bool(arguments["include_json_artifact"]):
        json_artifact_name = "ocr.json"
        output_file(json_artifact_name).write_text(
            json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        artifacts.append(
            {
                "path": f"/output/{json_artifact_name}",
                "media_type": "application/json",
                "name": json_artifact_name,
            }
        )
    output["text_artifact_name"] = text_artifact_name
    output["json_artifact_name"] = json_artifact_name
    progress.emit(
        "recognize.images",
        "OCR results ready",
        completed=len(selected),
        total=len(selected),
        unit="images",
    )
    return output, artifacts


def fail_result(invocation: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "invocation_id": invocation["invocation_id"],
        "operation_id": invocation["operation_id"],
        "status": "failed",
        "artifacts": [],
        "error": {"code": code, "message": message, "retryable": False},
    }


def invoke(invocation: dict[str, Any]) -> dict[str, Any]:
    try:
        if invocation["action"]["action_id"] != "ocr.recognize_images":
            raise OcrActionError("unsupported_action", "OCR action is unsupported")
        output, artifacts = recognize_images(invocation, Progress(invocation))
        return {
            "schema_version": 1,
            "invocation_id": invocation["invocation_id"],
            "operation_id": invocation["operation_id"],
            "status": "succeeded",
            "output": output,
            "artifacts": artifacts,
        }
    except OcrActionError as exc:
        for name in ("ocr.txt", "ocr.json"):
            (Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"]) / name).unlink(missing_ok=True)
        return fail_result(invocation, exc.code, str(exc)[:500])
    except (KeyError, TypeError, ValueError, OSError):
        for name in ("ocr.txt", "ocr.json"):
            (Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"]) / name).unlink(missing_ok=True)
        return fail_result(invocation, "ocr_processing_failed", "OCR processing failed")
    finally:
        shutil.rmtree(
            Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"]) / ".runtime-tmp",
            ignore_errors=True,
        )


def main() -> None:
    initialize = receive()
    response(
        initialize["id"],
        {
            "protocol_version": 1,
            "process_isolated": True,
            "access_isolated": os.environ.get("SHEJANE_PLUGIN_ACCESS_ISOLATED") == "1",
            "resource_isolated": os.environ.get("SHEJANE_PLUGIN_RESOURCE_ISOLATED") == "1",
            "sandboxed": os.environ.get("SHEJANE_PLUGIN_SANDBOXED") == "1",
        },
    )
    request = receive()
    response(request["id"], invoke(request["params"]))
    shutdown = receive()
    response(shutdown["id"], {})


if __name__ == "__main__":
    main()
