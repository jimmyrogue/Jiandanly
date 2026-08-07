from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from shejane_runtime.plugins.executor import ManagedWorkerActionExecutor
from shejane_runtime.plugins.macos_vm import load_macos_vm_resources
from shejane_runtime.plugins.ocr import OCRActionExecutor
from shejane_runtime.plugins.platforms import current_managed_worker_platform
from shejane_runtime.plugins.runtime_assets import RuntimeAssetHandle, RuntimeAssetStore

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "runtime" / "plugins" / "ocr" / "worker" / "ocr_worker.py"
ASSET_ENV = "SHEJANE_RAPIDOCR_RUNTIME_ASSET"
DARWIN_BUILDER = REPO_ROOT / "runtime" / "plugins" / "ocr" / "runtime-assets" / "build_darwin.py"


def test_darwin_codesign_targets_framework_bundle(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("ocr_darwin_builder", DARWIN_BUILDER)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    root = tmp_path / "bin"
    framework = root / "_internal" / "Python.framework"
    version = framework / "Versions" / "3.12"

    assert builder.codesign_target(framework / "Python", root) == framework
    assert builder.codesign_target(version / "Python", root) == version
    assert builder.codesign_target(root / "_internal" / "libpython.dylib", root) == (
        root / "_internal" / "libpython.dylib"
    )

    source = tmp_path / "asset"
    source.mkdir()
    (source / "Python").write_bytes(b"python")
    (source / "Current").symlink_to("Python")
    output = tmp_path / "runtime.shejane-runtime-asset"
    builder.pack_asset(source, output)
    with zipfile.ZipFile(output) as archive:
        current = archive.getinfo("Current")
        assert current.external_attr >> 16 == 0o120777
        assert archive.read("Current") == b"Python"


def font(size: int, *candidates: str) -> ImageFont.FreeTypeFont:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise AssertionError(f"required test font is unavailable: {candidates}")


def install_asset(data_dir: Path, archive: Path) -> RuntimeAssetHandle:
    return RuntimeAssetStore(data_dir).install(
        archive,
        target_platform=(
            None
            if os.environ.get("SHEJANE_TEST_MACOS_VM_ASSETS")
            else current_managed_worker_platform()
        ),
    )


def real_executor(asset: RuntimeAssetHandle) -> ManagedWorkerActionExecutor:
    frozen = os.environ.get("SHEJANE_TEST_OCR_WORKER")
    command = (frozen,) if frozen else (sys.executable, str(WORKER))
    vm_manifest = os.environ.get("SHEJANE_TEST_MACOS_VM_ASSETS")
    return ManagedWorkerActionExecutor(
        command,
        runtime_assets=(asset,),
        vm_resources=(
            load_macos_vm_resources(Path(vm_manifest).resolve(strict=True)) if vm_manifest else None
        ),
        package_root=Path(command[0]).resolve(strict=True).parent if vm_manifest else None,
    )


def invocation(source: Path) -> dict[str, object]:
    data = source.read_bytes()
    return {
        "schema_version": 1,
        "invocation_id": "b23e4567-e89b-42d3-a456-426614174001",
        "operation_id": "run_01:ocr.recognize_images:real-asset",
        "action": {
            "plugin_id": "org.shejane.ocr",
            "plugin_version": "0.1.4",
            "plugin_digest": "sha256:" + "b" * 64,
            "action_id": "ocr.recognize_images",
        },
        "arguments": {
            "input_ids": ["source"],
            "minimum_confidence": 0.5,
            "max_lines": 100,
            "max_characters": 1_000,
            "include_text_artifact": True,
            "include_json_artifact": True,
        },
        "inputs": [
            {
                "id": "source",
                "path": f"/input/source/{source.name}",
                "media_type": "image/png",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
        "grants": {"capabilities": ["input.read", "artifact.write"]},
        "limits": {"timeout_ms": 300_000, "memory_mb": 2048, "output_mb": 64},
        "environment": {"locale": "en-US", "timezone": "UTC"},
    }


def write_text_image(path: Path) -> None:
    image = Image.new("RGB", (1200, 240), "white")
    text_font = font(
        72,
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    ImageDraw.Draw(image).text((30, 70), "SheJane OCR 2026", font=text_font, fill="black")
    image.save(path, format="PNG", optimize=False)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get(ASSET_ENV), reason=f"{ASSET_ENV} is not set")
async def test_real_composite_asset_executes_its_bundled_worker(tmp_path: Path) -> None:
    archive = Path(os.environ[ASSET_ENV]).resolve(strict=True)
    asset = install_asset(tmp_path / "data", archive)
    executor = OCRActionExecutor(tmp_path, asset)
    input_root = tmp_path / "input"
    source = input_root / "source" / "text.png"
    source.parent.mkdir(parents=True)
    write_text_image(source)
    output_root = tmp_path / "output"
    output_root.mkdir()

    result = await executor.invoke(
        invocation(source),
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "succeeded", result.get("error", result)
    recognized = "".join(result["output"]["images"][0]["full_text"].split()).casefold()
    assert "shejaneocr2026" in recognized


def multi_invocation(sources: list[Path]) -> dict[str, object]:
    request = invocation(sources[0])
    input_ids = [f"source-{index}" for index in range(1, len(sources) + 1)]
    request["arguments"] = {
        **request["arguments"],  # type: ignore[dict-item]
        "input_ids": input_ids,
    }
    request["inputs"] = [
        {
            "id": input_id,
            "path": f"/input/source/{source.name}",
            "media_type": "image/png",
            "size_bytes": source.stat().st_size,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
        for input_id, source in zip(input_ids, sources, strict=True)
    ]
    return request


def write_quality_images(base: Path, rotated: Path) -> tuple[str, ...]:
    simplified_font = font(
        52,
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    )
    traditional_font = font(
        52,
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/mingliu.ttc",
    )
    japanese_font = font(
        52,
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "C:/Windows/Fonts/YuGothM.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
    )
    latin_font = font(
        56,
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    handwriting_font = font(
        54,
        "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
        "C:/Windows/Fonts/comic.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    )
    image = Image.new("RGB", (1400, 1080), "white")
    drawing = ImageDraw.Draw(image)
    drawing.text((40, 30), "简体中文测试", font=simplified_font, fill="black")
    drawing.text((40, 130), "繁體中文測試", font=traditional_font, fill="black")
    drawing.text((40, 230), "日本語テスト", font=japanese_font, fill="black")
    drawing.text((40, 350), "SHEJANE OCR TEST 2026", font=latin_font, fill="black")
    drawing.text((40, 480), "LOW CONTRAST TEXT", font=latin_font, fill=(145, 145, 145))
    drawing.text((40, 630), "LEFT COLUMN", font=latin_font, fill="black")
    drawing.text((760, 630), "RIGHT COLUMN", font=latin_font, fill="black")
    drawing.text((40, 810), "Handwriting Sample", font=handwriting_font, fill="black")
    image.save(base, format="PNG", optimize=False)

    rotated_image = Image.new("RGB", (1000, 220), "white")
    ImageDraw.Draw(rotated_image).text((40, 65), "ROTATED 180", font=latin_font, fill="black")
    rotated_image.rotate(180).save(rotated, format="PNG", optimize=False)
    return (
        "简体中文测试",
        "繁體中文測試",
        "日本語テスト",
        "shejaneocrtest2026",
        "lowcontrasttext",
        "leftcolumn",
        "rightcolumn",
        "handwritingsample",
        "rotated",
        "180",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get(ASSET_ENV), reason=f"{ASSET_ENV} is not set")
async def test_real_rapidocr_asset_recognizes_text_deterministically(tmp_path: Path) -> None:
    archive = Path(os.environ[ASSET_ENV]).resolve(strict=True)
    asset = install_asset(tmp_path / "data", archive)
    executor = real_executor(asset)
    input_root = tmp_path / "input"
    source = input_root / "source" / "text.png"
    source.parent.mkdir(parents=True)
    write_text_image(source)

    results = []
    artifact_digests = []
    for index in range(2):
        output_root = tmp_path / f"output-{index}"
        output_root.mkdir()
        result = await executor.invoke(
            invocation(source), input_root=input_root, output_root=output_root
        )
        assert result["status"] == "succeeded", result.get("error", result)
        recognized = "".join(result["output"]["images"][0]["full_text"].split()).casefold()
        assert "shejaneocr2026" in recognized
        results.append(result["output"])
        artifact_digests.append(
            (
                hashlib.sha256((output_root / "ocr.txt").read_bytes()).hexdigest(),
                hashlib.sha256((output_root / "ocr.json").read_bytes()).hexdigest(),
            )
        )

    assert results[0] == results[1]
    assert artifact_digests[0] == artifact_digests[1]


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get(ASSET_ENV), reason=f"{ASSET_ENV} is not set")
async def test_real_rapidocr_asset_multilingual_layout_and_rotation_gate(
    tmp_path: Path,
) -> None:
    archive = Path(os.environ[ASSET_ENV]).resolve(strict=True)
    asset = install_asset(tmp_path / "data", archive)
    executor = real_executor(asset)
    input_root = tmp_path / "input"
    base = input_root / "source" / "quality.png"
    rotated = input_root / "source" / "rotated.png"
    base.parent.mkdir(parents=True)
    expected_markers = write_quality_images(base, rotated)
    output_root = tmp_path / "output"
    output_root.mkdir()

    result = await executor.invoke(
        multi_invocation([base, rotated]), input_root=input_root, output_root=output_root
    )

    assert result["status"] == "succeeded", result.get("error", result)
    recognized = [image["full_text"] for image in result["output"]["images"]]
    normalized = " ".join(recognized).replace(" ", "").casefold()
    for expected in expected_markers:
        assert expected in normalized, recognized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x89PNG\r\n\x1a\n" + b"\0" * 65_536,
        b"GIF89a\xff\xff\xff\xff" + b"\0" * 64,
    ],
    ids=("empty", "truncated-png", "oversized-gif"),
)
@pytest.mark.skipif(not os.environ.get(ASSET_ENV), reason=f"{ASSET_ENV} is not set")
async def test_real_rapidocr_asset_hostile_images_fail_closed(
    tmp_path: Path, payload: bytes
) -> None:
    archive = Path(os.environ[ASSET_ENV]).resolve(strict=True)
    asset = install_asset(tmp_path / "data", archive)
    executor = real_executor(asset)
    input_root = tmp_path / "input"
    source = input_root / "source" / "hostile.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    output_root = tmp_path / "output"
    output_root.mkdir()

    result = await executor.invoke(
        invocation(source), input_root=input_root, output_root=output_root
    )

    assert result["status"] == "failed", result
    assert result["artifacts"] == []
    assert not any(path.is_file() for path in output_root.rglob("*"))


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get(ASSET_ENV) or not os.environ.get("SHEJANE_TEST_MACOS_VM_ASSETS"),
    reason="real packaged VM assets are required",
)
async def test_real_rapidocr_asset_cancellation_discards_partial_output(tmp_path: Path) -> None:
    archive = Path(os.environ[ASSET_ENV]).resolve(strict=True)
    asset = install_asset(tmp_path / "data", archive)
    executor = real_executor(asset)
    input_root = tmp_path / "input"
    source = input_root / "source" / "text.png"
    source.parent.mkdir(parents=True)
    write_text_image(source)
    output_root = tmp_path / "output"
    output_root.mkdir()
    recognition_started = asyncio.Event()

    def on_progress(event: dict[str, object]) -> None:
        if event.get("phase") == "recognize.images":
            recognition_started.set()

    task = asyncio.create_task(
        executor.invoke(
            invocation(source),
            input_root=input_root,
            output_root=output_root,
            on_progress=on_progress,
        )
    )
    await asyncio.wait_for(recognition_started.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not any(path.is_file() for path in output_root.rglob("*"))

    replay_output = tmp_path / "replay-output"
    replay_output.mkdir()
    replay = await executor.invoke(
        invocation(source), input_root=input_root, output_root=replay_output
    )
    assert replay["status"] == "succeeded", replay
