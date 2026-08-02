"""CLI entry point for the standalone SheJane Runtime."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from .config import get_settings
from .crash_reporting import install_local_crash_reporting
from .observability import configure_logging
from .plugins.macos_vm import load_macos_vm_resources
from .server import create_app


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="shejane-runtime")
    parser.add_argument("--host", help="loopback listener address")
    parser.add_argument("--port", type=int, help="loopback listener port")
    parser.add_argument("--token", help="Client pairing token")
    parser.add_argument("--data-dir", type=Path, help="Runtime data directory")
    parser.add_argument(
        "--managed-worker-vm-assets",
        type=Path,
        help="bundled macOS Managed Worker VM asset manifest",
    )
    parser.add_argument(
        "--managed-worker-linux-assets",
        type=Path,
        help="bundled Linux Managed Worker asset manifest",
    )
    parser.add_argument(
        "--computer-use-package",
        type=Path,
        help="bundled fixed Computer Use capability package",
    )
    parser.add_argument(
        "--browser-qa-package",
        type=Path,
        help="bundled fixed Browser QA capability package",
    )
    parser.add_argument(
        "--browser-qa-runtime-asset",
        type=Path,
        help="bundled fixed Browser QA Runtime Asset",
    )
    parser.add_argument(
        "--ocr-package",
        type=Path,
        help="bundled fixed OCR capability package",
    )
    parser.add_argument(
        "--ocr-runtime-asset",
        type=Path,
        help="bundled fixed RapidOCR Runtime Asset",
    )
    parser.add_argument(
        "--fixed-runtime-asset-base-url",
        help="approved HTTPS base URL for on-demand fixed Runtime Assets",
    )
    parser.add_argument(
        "--validate-managed-worker-vm-assets",
        action="store_true",
        help="validate the bundled macOS VM asset set and exit",
    )
    parser.add_argument("--crash-report-self-test", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    install_local_crash_reporting(os.environ.get("SHEJANE_RUNTIME_CRASH_DIRECTORY"))
    if args.crash_report_self_test:
        os.abort()
    configure_logging()
    settings = get_settings()
    overrides = {
        key: value
        for key, value in {
            "host": args.host,
            "port": args.port,
            "pairing_token": args.token,
            "data_dir": args.data_dir,
            "managed_worker_vm_assets": args.managed_worker_vm_assets,
            "managed_worker_linux_assets": args.managed_worker_linux_assets,
            "computer_use_package": args.computer_use_package,
            "browser_qa_package": args.browser_qa_package,
            "browser_qa_runtime_asset": args.browser_qa_runtime_asset,
            "ocr_package": args.ocr_package,
            "ocr_runtime_asset": args.ocr_runtime_asset,
            "fixed_runtime_asset_base_url": args.fixed_runtime_asset_base_url,
        }.items()
        if value is not None
    }
    if overrides:
        settings = settings.model_copy(update=overrides)
        settings = type(settings).model_validate(settings.model_dump())
    if args.validate_managed_worker_vm_assets:
        if settings.managed_worker_vm_assets is None:
            raise SystemExit("--managed-worker-vm-assets is required for validation")
        load_macos_vm_resources(settings.managed_worker_vm_assets)
        return 0
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
