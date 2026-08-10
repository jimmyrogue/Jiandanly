"""Managed Worker JSON-RPC framing, validation, and process shutdown."""

from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any

from ..processes import kill_process_tree


class WorkerProtocolError(RuntimeError):
    """The worker violated the v1 control protocol."""


async def _write_frame(
    writer: asyncio.StreamWriter,
    payload: dict[str, Any],
    max_frame_bytes: int,
) -> None:
    frame = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(frame) > max_frame_bytes:
        raise WorkerProtocolError("managed worker outbound frame limit exceeded")
    writer.write(frame)
    await writer.drain()


async def _request_cooperative_cancel(
    process: asyncio.subprocess.Process,
    writer: asyncio.StreamWriter,
    invocation: dict[str, Any],
    *,
    reason: str,
    max_frame_bytes: int,
    grace_seconds: float,
) -> bool:
    if process.returncode is not None:
        return True
    try:
        async with asyncio.timeout(max(0.01, grace_seconds)):
            await _write_frame(
                writer,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "cancel",
                    "params": {
                        "operation_id": invocation["operation_id"],
                        "reason": reason,
                    },
                },
                max_frame_bytes,
            )
            await process.wait()
            return True
    except (BrokenPipeError, ConnectionError, TimeoutError):
        return process.returncode is not None


def _progress_notification(
    frame: dict[str, Any],
    *,
    invocation: dict[str, Any],
    previous_sequence: int,
) -> int:
    if set(frame) != {"jsonrpc", "method", "params"} or frame.get("jsonrpc") != "2.0":
        raise WorkerProtocolError("managed worker progress notification is invalid")
    if frame.get("method") != "notifications/progress" or not isinstance(frame.get("params"), dict):
        raise WorkerProtocolError("managed worker notification method is unsupported")
    params = frame["params"]
    required = {"schema_version", "invocation_id", "operation_id", "sequence", "phase"}
    optional = {"message", "completed", "total", "unit"}
    if not required <= set(params) or set(params) - required - optional:
        raise WorkerProtocolError("managed worker progress payload is invalid")
    if (
        params["schema_version"] != 1
        or params["invocation_id"] != invocation["invocation_id"]
        or params["operation_id"] != invocation["operation_id"]
    ):
        raise WorkerProtocolError("managed worker progress identity changed")
    sequence = params["sequence"]
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence != previous_sequence + 1
    ):
        raise WorkerProtocolError("managed worker progress sequence is invalid")
    phase = params["phase"]
    message = params.get("message")
    unit = params.get("unit")
    if not isinstance(phase, str) or re.fullmatch(r"[a-z][a-z0-9._-]{0,99}", phase) is None:
        raise WorkerProtocolError("managed worker progress phase is invalid")
    if message is not None and (not isinstance(message, str) or len(message) > 500):
        raise WorkerProtocolError("managed worker progress message is invalid")
    if unit is not None and (not isinstance(unit, str) or not unit or len(unit) > 64):
        raise WorkerProtocolError("managed worker progress unit is invalid")
    completed = params.get("completed")
    total = params.get("total")
    if total is not None and completed is None:
        raise WorkerProtocolError("managed worker progress total requires completed")
    for value in (completed, total):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise WorkerProtocolError("managed worker progress value is invalid")
    if total is not None and (total <= 0 or completed > total):
        raise WorkerProtocolError("managed worker progress range is invalid")
    return sequence


def _vision_request(frame: dict[str, Any], invocation: dict[str, Any]) -> dict[str, Any]:
    if set(frame) != {"jsonrpc", "id", "method", "params"} or frame.get("jsonrpc") != "2.0":
        raise WorkerProtocolError("managed worker host request is invalid")
    request_id = frame.get("id")
    if (
        not isinstance(request_id, str)
        or re.fullmatch(r"worker:[a-z0-9._:-]{1,80}", request_id) is None
    ):
        raise WorkerProtocolError("managed worker host request id is invalid")
    if frame.get("method") != "model/vision/invoke":
        raise WorkerProtocolError("managed worker host request method is unsupported")
    if "model.vision.invoke" not in invocation["grants"]["capabilities"]:
        raise WorkerProtocolError("managed worker vision host call was not granted")
    params = frame.get("params")
    required = {"model_binding_id", "input_ids", "task", "prompt", "max_output_tokens"}
    optional = {"temperature", "detail"}
    if (
        not isinstance(params, dict)
        or set(params) - required - optional
        or not required <= set(params)
    ):
        raise WorkerProtocolError("managed worker vision host-call params are invalid")
    binding_id = params["model_binding_id"]
    if (
        not isinstance(binding_id, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", binding_id) is None
    ):
        raise WorkerProtocolError("managed worker vision model binding is invalid")
    if binding_id != invocation.get("model_binding_id"):
        raise WorkerProtocolError("managed worker vision model binding changed")
    input_ids = params["input_ids"]
    authorized_ids = {item.get("id") for item in invocation.get("inputs", [])}
    if (
        not isinstance(input_ids, list)
        or not 1 <= len(input_ids) <= 16
        or len(input_ids) != len(set(input_ids))
        or any(
            not isinstance(input_id, str) or len(input_id) > 128 or input_id not in authorized_ids
            for input_id in input_ids
        )
    ):
        raise WorkerProtocolError("managed worker vision inputs are invalid")
    if params["task"] not in {"describe", "question"}:
        raise WorkerProtocolError("managed worker vision task is invalid")
    prompt = params["prompt"]
    if not isinstance(prompt, str) or not prompt or len(prompt) > 8_000:
        raise WorkerProtocolError("managed worker vision prompt is invalid")
    max_output_tokens = params["max_output_tokens"]
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= 8_192
    ):
        raise WorkerProtocolError("managed worker vision output limit is invalid")
    temperature = params.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
        or not 0 <= temperature <= 2
    ):
        raise WorkerProtocolError("managed worker vision temperature is invalid")
    if params.get("detail") not in {None, "auto", "low", "high", "original"}:
        raise WorkerProtocolError("managed worker vision detail is invalid")
    return dict(params)


async def _read_frame(
    reader: asyncio.StreamReader,
    max_frame_bytes: int,
) -> dict[str, Any]:
    try:
        frame = await reader.readline()
    except ValueError as exc:
        raise WorkerProtocolError("managed worker inbound frame limit exceeded") from exc
    if not frame:
        raise WorkerProtocolError("managed worker closed stdout before responding")
    if len(frame) > max_frame_bytes or not frame.endswith(b"\n"):
        raise WorkerProtocolError("managed worker inbound frame limit exceeded")
    try:
        payload = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("managed worker emitted invalid JSON") from exc
    if not isinstance(payload, dict):
        raise WorkerProtocolError("managed worker frame must be an object")
    return payload


async def _bounded_stderr(reader: asyncio.StreamReader, limit: int) -> bytes:
    captured = bytearray()
    while chunk := await reader.read(8192):
        if len(captured) < limit:
            captured.extend(chunk[: limit - len(captured)])
    return bytes(captured)


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    allow_supervisor_cleanup: bool = False,
) -> None:
    if process.returncode is not None:
        return
    if allow_supervisor_cleanup:
        process.terminate()
        try:
            async with asyncio.timeout(2):
                await process.wait()
            return
        except TimeoutError:
            pass
    await kill_process_tree(process)


def _validate_result_identity(result: Any, invocation: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise WorkerProtocolError("managed worker result must be an object")
    if result.get("invocation_id") != invocation["invocation_id"]:
        raise WorkerProtocolError("managed worker changed invocation_id")
    if result.get("operation_id") != invocation["operation_id"]:
        raise WorkerProtocolError("managed worker changed operation_id")
    if result.get("status") not in {"succeeded", "failed"}:
        raise WorkerProtocolError("managed worker returned an invalid status")


def _validate_staged_artifacts(
    result: dict[str, Any],
    output_root: Path,
    output_limit_mb: int,
) -> None:
    total = 0
    for artifact in result.get("artifacts", []):
        try:
            relative = PurePosixPath(str(artifact["path"])).relative_to("/output")
        except (KeyError, ValueError) as exc:
            raise WorkerProtocolError("artifact is outside /output") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise WorkerProtocolError("artifact path is unsafe")
        candidate = output_root.joinpath(*relative.parts)
        current = output_root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise WorkerProtocolError("artifact path contains a symlink")
        try:
            candidate.resolve(strict=True).relative_to(output_root)
        except (FileNotFoundError, ValueError) as exc:
            raise WorkerProtocolError("artifact does not resolve inside /output") from exc
        if not candidate.is_file():
            raise WorkerProtocolError("artifact candidate is not a regular file")
        total += candidate.stat().st_size
    if total > int(output_limit_mb) * 1024 * 1024:
        raise WorkerProtocolError("artifact output limit exceeded")
