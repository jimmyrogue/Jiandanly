#!/usr/bin/env python3
"""One-shot Vision Managed Worker for explicit local or cloud backends."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

LOCAL_PLUGIN_ID = "org.shejane.vision.local"
CLOUD_PLUGIN_ID = "org.shejane.vision.cloud"
LOCAL_ASSET_ID = "org.llama-mtmd.runtime"
SUPPORTED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_INPUTS = 16
MAX_ENGINE_RESPONSE_BYTES = 512 * 1024
MAX_TEXT_CHARS = 262_144


class VisionActionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def send(value: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def response(request_id: int, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


class Progress:
    def __init__(self, invocation: dict[str, Any]) -> None:
        self.invocation = invocation
        self.sequence = 0

    def emit(self, phase: str, message: str) -> None:
        self.sequence += 1
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {
                    "schema_version": 1,
                    "invocation_id": self.invocation["invocation_id"],
                    "operation_id": self.invocation["operation_id"],
                    "sequence": self.sequence,
                    "phase": phase,
                    "message": message,
                },
            }
        )


def contained_file(root: Path, virtual_path: str) -> Path:
    try:
        relative = PurePosixPath(virtual_path).relative_to("/input")
    except ValueError as exc:
        raise VisionActionError(
            "invalid_input", "Vision input path is invalid"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise VisionActionError("invalid_input", "Vision input path is invalid")
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise VisionActionError("invalid_input", "Vision input is unavailable")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise VisionActionError("invalid_input", "Vision input is unavailable") from exc
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
        or len(input_ids) != len(set(input_ids))
    ):
        raise VisionActionError("invalid_input", "Vision input selection is invalid")
    available = {item["id"]: item for item in invocation["inputs"]}
    input_root = Path(os.environ["SHEJANE_PLUGIN_INPUT_ROOT"])
    selected: list[dict[str, Any]] = []
    for input_id in input_ids:
        reference = available.get(input_id)
        if (
            not isinstance(reference, dict)
            or reference.get("media_type") not in SUPPORTED_MEDIA_TYPES
        ):
            raise VisionActionError(
                "invalid_input", "selected Vision input is unavailable"
            )
        source = contained_file(input_root, str(reference["path"]))
        size = source.stat().st_size
        digest = sha256_file(source)
        if size != reference.get("size_bytes") or digest != reference.get("sha256"):
            raise VisionActionError(
                "invalid_input", "selected Vision input identity changed"
            )
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


def runtime_temp() -> Path:
    temporary = output_root() / ".runtime-tmp"
    temporary.mkdir(mode=0o700, exist_ok=True)
    temporary.resolve(strict=True).relative_to(output_root())
    return temporary


def normalized_text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_CHARS:
        raise VisionActionError(
            "engine_protocol_violation", "Vision response text is invalid"
        )
    text = "".join(
        character
        if character in "\t\n\r" or unicodedata.category(character) != "Cc"
        else ""
        for character in value
    ).strip()
    if not text:
        raise VisionActionError(
            "engine_protocol_violation", "Vision response text is invalid"
        )
    return text


def normalized_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise VisionActionError("engine_protocol_violation", "Vision usage is invalid")
    if set(value) - {"input_tokens", "output_tokens", "total_tokens"}:
        raise VisionActionError("engine_protocol_violation", "Vision usage is invalid")
    result: dict[str, int] = {}
    for key, item in value.items():
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= 10_000_000
        ):
            raise VisionActionError(
                "engine_protocol_violation", "Vision usage is invalid"
            )
        result[key] = item
    return result


def normalized_warnings(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise VisionActionError(
            "engine_protocol_violation", "Vision warnings are invalid"
        )
    if any(not isinstance(item, str) or not item or len(item) > 500 for item in value):
        raise VisionActionError(
            "engine_protocol_violation", "Vision warnings are invalid"
        )
    return list(value)


def local_asset(initialize: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    assets = initialize.get("runtime_assets")
    if not isinstance(assets, list) or len(assets) != 1:
        raise VisionActionError(
            "runtime_unavailable", "local Vision Runtime Asset is unavailable"
        )
    reference = assets[0]
    if not isinstance(reference, dict) or reference.get("id") != LOCAL_ASSET_ID:
        raise VisionActionError(
            "runtime_unavailable", "local Vision Runtime Asset is unavailable"
        )
    try:
        roots = json.loads(os.environ["SHEJANE_PLUGIN_RUNTIME_ASSETS"])
        payload = Path(roots[LOCAL_ASSET_ID]).resolve(strict=True)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise VisionActionError(
            "runtime_unavailable", "local Vision Runtime Asset is unavailable"
        ) from exc
    engine = (
        payload / "bin" / ("vision-engine.exe" if os.name == "nt" else "vision-engine")
    )
    if engine.is_symlink() or not engine.is_file():
        raise VisionActionError(
            "runtime_unavailable", "local Vision engine is unavailable"
        )
    try:
        engine.resolve(strict=True).relative_to(payload)
    except (OSError, ValueError) as exc:
        raise VisionActionError(
            "runtime_unavailable", "local Vision engine is unavailable"
        ) from exc
    identity = {
        "runtime_asset_id": str(reference["id"]),
        "runtime_asset_version": str(reference["version"]),
        "runtime_asset_digest": str(reference["digest"]),
    }
    return engine, identity


def local_inference(
    initialize: dict[str, Any],
    invocation: dict[str, Any],
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    engine, asset_identity = local_asset(initialize)
    temporary = runtime_temp()
    request_path = temporary / "request.json"
    response_path = temporary / "response.json"
    arguments = invocation["arguments"]
    request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "inputs": selected,
                "task": arguments["task"],
                "prompt": arguments["prompt"],
                "max_output_tokens": arguments["max_output_tokens"],
                "temperature": arguments["temperature"],
                "detail": arguments["detail"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    process = subprocess.run(
        [str(engine), str(request_path), str(response_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "SystemRoot", "WINDIR"}
        },
        check=False,
    )
    if process.returncode != 0:
        raise VisionActionError("vision_failed", "local Vision engine failed")
    if response_path.is_symlink() or not response_path.is_file():
        raise VisionActionError(
            "engine_protocol_violation", "local Vision response is unavailable"
        )
    if response_path.stat().st_size > MAX_ENGINE_RESPONSE_BYTES:
        raise VisionActionError(
            "engine_protocol_violation", "local Vision response is too large"
        )
    try:
        result = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisionActionError(
            "engine_protocol_violation", "local Vision response is invalid"
        ) from exc
    if not isinstance(result, dict) or set(result) != {
        "text",
        "model_id",
        "usage",
        "warnings",
    }:
        raise VisionActionError(
            "engine_protocol_violation", "local Vision response is invalid"
        )
    model_id = result["model_id"]
    if not isinstance(model_id, str) or not model_id or len(model_id) > 200:
        raise VisionActionError(
            "engine_protocol_violation", "local Vision model identity is invalid"
        )
    return {
        "text": normalized_text(result["text"]),
        "model": {
            "binding_id": asset_identity["runtime_asset_digest"],
            "model_id": model_id,
            **asset_identity,
        },
        "usage": normalized_usage(result["usage"]),
        "warnings": normalized_warnings(result["warnings"]),
    }


def cloud_inference(
    invocation: dict[str, Any], selected: list[dict[str, Any]]
) -> dict[str, Any]:
    binding_id = invocation.get("model_binding_id")
    if not isinstance(binding_id, str) or not binding_id:
        raise VisionActionError(
            "model_binding_unavailable", "cloud Vision binding is unavailable"
        )
    arguments = invocation["arguments"]
    request_id = "worker:vision:1"
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "model/vision/invoke",
            "params": {
                "model_binding_id": binding_id,
                "input_ids": [item["id"] for item in selected],
                "task": arguments["task"],
                "prompt": arguments["prompt"],
                "max_output_tokens": arguments["max_output_tokens"],
                "temperature": arguments["temperature"],
                "detail": arguments["detail"],
            },
        }
    )
    try:
        frame = json.loads(sys.stdin.readline())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisionActionError(
            "host_protocol_violation", "Vision host response is invalid"
        ) from exc
    if (
        not isinstance(frame, dict)
        or frame.get("jsonrpc") != "2.0"
        or frame.get("id") != request_id
        or set(frame) != {"jsonrpc", "id", "result"}
        or not isinstance(frame["result"], dict)
    ):
        raise VisionActionError(
            "host_protocol_violation", "Vision host response is invalid"
        )
    result = frame["result"]
    if set(result) != {"text", "model", "usage"} or not isinstance(
        result["model"], dict
    ):
        raise VisionActionError(
            "host_protocol_violation", "Vision host response is invalid"
        )
    model = result["model"]
    if set(model) != {"connection_id", "connection_version", "model_id"}:
        raise VisionActionError(
            "host_protocol_violation", "Vision model identity is invalid"
        )
    if (
        not isinstance(model["connection_id"], str)
        or not model["connection_id"]
        or isinstance(model["connection_version"], bool)
        or not isinstance(model["connection_version"], int)
        or model["connection_version"] < 1
        or not isinstance(model["model_id"], str)
        or not model["model_id"]
    ):
        raise VisionActionError(
            "host_protocol_violation", "Vision model identity is invalid"
        )
    return {
        "text": normalized_text(result["text"]),
        "model": {"binding_id": binding_id, **model},
        "usage": normalized_usage(result["usage"]),
        "warnings": ["Image content was processed by the configured remote provider."],
    }


def write_artifacts(
    output: dict[str, Any], arguments: dict[str, Any]
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    text_name: str | None = None
    json_name: str | None = None
    if bool(arguments["include_text_artifact"]):
        text_name = "vision.txt"
        (output_root() / text_name).write_text(output["text"] + "\n", encoding="utf-8")
        artifacts.append(
            {
                "path": f"/output/{text_name}",
                "media_type": "text/plain",
                "name": text_name,
            }
        )
    if bool(arguments["include_json_artifact"]):
        json_name = "vision.json"
        body = {
            **output,
            "text_artifact_name": text_name,
            "json_artifact_name": json_name,
        }
        (output_root() / json_name).write_text(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        artifacts.append(
            {
                "path": f"/output/{json_name}",
                "media_type": "application/json",
                "name": json_name,
            }
        )
    output["text_artifact_name"] = text_name
    output["json_artifact_name"] = json_name
    return artifacts


def fail_result(invocation: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "invocation_id": invocation["invocation_id"],
        "operation_id": invocation["operation_id"],
        "status": "failed",
        "artifacts": [],
        "error": {"code": code, "message": message, "retryable": False},
    }


def invoke(initialize: dict[str, Any], invocation: dict[str, Any]) -> dict[str, Any]:
    try:
        if invocation["action"]["action_id"] != "vision.analyze_images":
            raise VisionActionError(
                "unsupported_action", "Vision action is unsupported"
            )
        backend = invocation["arguments"]["backend"]
        plugin_id = invocation["action"]["plugin_id"]
        if (backend, plugin_id) not in {
            ("local", LOCAL_PLUGIN_ID),
            ("cloud", CLOUD_PLUGIN_ID),
        }:
            raise VisionActionError(
                "backend_mismatch", "Vision backend does not match the plugin"
            )
        selected = selected_inputs(invocation)
        progress = Progress(invocation)
        progress.emit("vision.inference", "Analyzing selected images")
        result = (
            local_inference(initialize, invocation, selected)
            if backend == "local"
            else cloud_inference(invocation, selected)
        )
        output = {
            "backend": backend,
            "input_ids": [item["id"] for item in selected],
            **result,
        }
        artifacts = write_artifacts(output, invocation["arguments"])
        progress.emit("vision.complete", "Vision result ready")
        return {
            "schema_version": 1,
            "invocation_id": invocation["invocation_id"],
            "operation_id": invocation["operation_id"],
            "status": "succeeded",
            "output": output,
            "artifacts": artifacts,
        }
    except VisionActionError as exc:
        for name in ("vision.txt", "vision.json"):
            (Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"]) / name).unlink(
                missing_ok=True
            )
        return fail_result(invocation, exc.code, str(exc)[:500])
    except (KeyError, TypeError, ValueError, OSError):
        for name in ("vision.txt", "vision.json"):
            (Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"]) / name).unlink(
                missing_ok=True
            )
        return fail_result(
            invocation, "vision_processing_failed", "Vision processing failed"
        )
    finally:
        shutil.rmtree(
            Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"]) / ".runtime-tmp",
            ignore_errors=True,
        )


def main() -> None:
    initialize_request = json.loads(sys.stdin.readline())
    initialize = initialize_request["params"]
    response(
        initialize_request["id"],
        {
            "protocol_version": 1,
            "process_isolated": True,
            "access_isolated": os.environ.get("SHEJANE_PLUGIN_ACCESS_ISOLATED") == "1",
            "resource_isolated": os.environ.get("SHEJANE_PLUGIN_RESOURCE_ISOLATED") == "1",
            "sandboxed": os.environ.get("SHEJANE_PLUGIN_SANDBOXED") == "1",
        },
    )
    request = json.loads(sys.stdin.readline())
    response(request["id"], invoke(initialize, request["params"]))
    shutdown = json.loads(sys.stdin.readline())
    response(shutdown["id"], {})


if __name__ == "__main__":
    main()
