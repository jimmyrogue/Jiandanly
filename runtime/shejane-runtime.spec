# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the SheJane Runtime.
#
# Produces a self-contained ONEDIR bundle (dist/shejane-runtime/) that runs WITHOUT a
# system Python — it's what the Electron desktop app spawns. Build it with
# `make package-runtime` (= isolated `uv run --group package ... PyInstaller`).
#
# onedir (not onefile) on purpose: onefile re-extracts to a temp dir on every
# launch (slow + AV re-scan) and breaks uvicorn's signal handling; onedir starts
# fast and lets each inner binary be code-signed individually (needed for macOS
# notarization in a later phase).

import sys
import platform
from importlib.util import find_spec
from pathlib import Path
from shutil import copy2

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

datas = []
binaries = []
hiddenimports = []

if sys.platform.startswith("linux"):
    linux_launcher = Path("build/managed-worker-linux/shejane-managed-worker-linux")
    linux_bubblewrap = Path("build/managed-worker-linux/bubblewrap")
    if not linux_launcher.is_file():
        raise SystemExit("Linux Managed Worker launcher must be built before PyInstaller")
    if not (linux_bubblewrap / "shejane-bwrap").is_file():
        raise SystemExit("Linux Managed Worker bubblewrap must be built before PyInstaller")
    binaries.append((str(linux_launcher), "."))
    binaries += [
        (str(linux_bubblewrap / "shejane-bwrap"), "managed-worker-linux"),
        (str(linux_bubblewrap / "libcap.so.2"), "managed-worker-linux"),
    ]
    datas += [
        (str(linux_bubblewrap / name), "managed-worker-linux")
        for name in ("COPYING.bubblewrap", "copyright.libcap", "manifest.json")
    ]

# Packages PyInstaller's static analysis under-collects because they rely on
# dynamic imports, entry-point discovery, or ship data files / native libs.
# markitdown builds a module-level MarkItDown() which loads magika's ONNX model
# via onnxruntime at import time. Keep the inference runtime, but not ONNX's
# quantization/conversion toolchain.
datas += collect_data_files(
    "onnxruntime",
    includes=["LICENSE", "ThirdPartyNotices.txt"],
)
binaries += collect_dynamic_libs("onnxruntime")
hiddenimports += collect_submodules("onnxruntime.capi")
for pkg in (
    "magika",
    "langgraph",
    "langchain",
    "langchain_core",
    "deepagents",
    "markitdown",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

def is_wasmtime_library(path):
    name = Path(path).name
    return name.startswith("_libwasmtime.") or name == "_wasmtime.dll"


# wasmtime resolves its platform library with ctypes from an exact package-relative
# path. PyInstaller reclassifies the file differently across platforms, so exclude
# it from automatic collection and restore it after COLLECT at the required path.
d, b, h = collect_all("wasmtime")
datas += [entry for entry in d if not is_wasmtime_library(entry[0])]
binaries += [entry for entry in b if not is_wasmtime_library(entry[0])]
hiddenimports += h
wasmtime_spec = find_spec("wasmtime")
if wasmtime_spec is None or wasmtime_spec.origin is None:
    raise SystemExit("wasmtime package must be installed before PyInstaller")
wasmtime_root = Path(wasmtime_spec.origin).parent
wasmtime_library = next(
    (path for path in wasmtime_root.glob("*/*") if is_wasmtime_library(path)),
    None,
)
if wasmtime_library is None:
    raise SystemExit("wasmtime platform library is missing from the installed wheel")

# The runtime boots via uvicorn.run("shejane_runtime.server:app", ...) — a STRING
# import — so the whole shejane_runtime package is invisible to static analysis.
hiddenimports += collect_submodules("shejane_runtime")
datas += [
    (str(path), "shejane_runtime/agent/prompts")
    for path in Path("src/shejane_runtime/agent/prompts").glob("*.md")
]
if sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
    computer_use_package = Path(
        "plugins/computer-use/dist/computer-use-0.2.1-darwin-arm64.shejane-plugin"
    )
    if not computer_use_package.is_file():
        raise SystemExit("Computer Use fixed capability package must be built before PyInstaller")
    datas.append((str(computer_use_package), "builtin-plugins"))
    browser_qa_package = Path(
        "plugins/browser-qa/dist/browser-qa-0.1.2-darwin-arm64.shejane-plugin"
    )
    if not browser_qa_package.is_file():
        raise SystemExit("Browser QA fixed capability package must be built before PyInstaller")
    datas.append((str(browser_qa_package), "builtin-plugins"))
    ocr_package = Path(
        "plugins/ocr/dist/ocr-0.1.2-darwin-arm64.shejane-plugin"
    )
    if not ocr_package.is_file():
        raise SystemExit("OCR fixed capability package must be built before PyInstaller")
    datas.append((str(ocr_package), "builtin-plugins"))
elif sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}:
    browser_qa_package = Path(
        "plugins/browser-qa/dist/browser-qa-0.1.2-windows-amd64.shejane-plugin"
    )
    if not browser_qa_package.is_file():
        raise SystemExit("Windows Browser QA fixed capability package must be built before PyInstaller")
    datas.append((str(browser_qa_package), "builtin-plugins"))
    ocr_package = Path(
        "plugins/ocr/dist/ocr-0.1.2-windows-amd64.shejane-plugin"
    )
    if not ocr_package.is_file():
        raise SystemExit("Windows OCR fixed capability package must be built before PyInstaller")
    datas.append((str(ocr_package), "builtin-plugins"))
# uvicorn loads its loop / protocol / lifespan implementations dynamically.
hiddenimports += collect_submodules("uvicorn")

a = Analysis(
    ["pyi_entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Browser QA carries pinned Playwright code and Chromium in its fixed package
    # and Runtime Asset. Never absorb a developer's ambient copy into Runtime.
    excludes=[
        "_pytest",
        "browser_use",
        "coverage",
        "mypy",
        "onnxruntime.backend",
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "onnxruntime.transformers",
        "playwright",
        "pytest",
        "ruff",
        "sympy",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir: deps live beside the exe, not inside it
    name="shejane-runtime",
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # native arch of the build machine (per-OS/arch in CI)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="shejane-runtime",  # → dist/shejane-runtime/
)

wasmtime_destination = (
    Path(DISTPATH)
    / "shejane-runtime"
    / "_internal"
    / "wasmtime"
    / wasmtime_library.parent.name
    / wasmtime_library.name
)
wasmtime_destination.parent.mkdir(parents=True, exist_ok=True)
copy2(wasmtime_library, wasmtime_destination)
