"""Runtime-owned host adapter for the fixed OCR plugin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .executor import ManagedWorkerActionExecutor
from .platforms import prepare_managed_worker_entrypoint
from .runtime_assets import RuntimeAssetHandle

OCR_PLUGIN_ID = "org.shejane.ocr"
OCR_PLUGIN_VERSION = "0.1.5"


def is_allowed_ocr_package(*, plugin_id: str, version: str, handler: str) -> bool:
    return plugin_id == OCR_PLUGIN_ID and version == OCR_PLUGIN_VERSION and handler == "ocr"


class OCRActionExecutor:
    """Execute SheJane's trusted OCR Worker against one pinned native asset."""

    def __init__(self, package_root: Path, runtime_asset: RuntimeAssetHandle) -> None:
        del package_root
        relative_entrypoint = (
            "worker/ocr-worker.exe"
            if runtime_asset.platform.startswith("windows/")
            else "worker/ocr-worker"
        )
        entrypoint = prepare_managed_worker_entrypoint(
            runtime_asset.payload,
            relative_entrypoint,
        )
        self._executor = ManagedWorkerActionExecutor(
            (str(entrypoint),),
            runtime_assets=(runtime_asset,),
        )

    async def invoke(
        self,
        invocation: dict[str, Any],
        *,
        input_root: Path,
        output_root: Path,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        return await self._executor.invoke(
            invocation,
            input_root=input_root,
            output_root=output_root,
            on_progress=on_progress,
        )
