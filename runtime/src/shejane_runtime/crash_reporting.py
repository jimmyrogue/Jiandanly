"""Local-only native Runtime crash reporting."""

from __future__ import annotations

import atexit
import faulthandler
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

_crash_stream: TextIO | None = None


def install_local_crash_reporting(directory: str | None) -> TextIO | None:
    """Install faulthandler into a private local file, never a network sink."""
    global _crash_stream

    if not directory:
        return None
    crash_directory = Path(directory)
    if not crash_directory.is_absolute() or crash_directory.is_symlink():
        return None

    try:
        crash_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        filename = f"runtime-native-{os.getpid()}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.log"
        descriptor = os.open(
            crash_directory / filename,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        faulthandler.enable(file=stream, all_threads=True)
    except OSError:
        return None

    _crash_stream = stream
    atexit.register(stream.close)
    return stream
