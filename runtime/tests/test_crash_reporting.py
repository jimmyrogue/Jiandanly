from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from shejane_runtime.crash_reporting import install_local_crash_reporting


def test_native_crash_reporter_uses_a_private_local_file(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}

    def enable(*, file: Any, all_threads: bool) -> None:
        captured.update(file=file, all_threads=all_threads)

    monkeypatch.setattr("shejane_runtime.crash_reporting.faulthandler.enable", enable)
    monkeypatch.setattr(
        Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("path reopen"))
    )

    stream = install_local_crash_reporting(str(tmp_path))

    assert stream is not None
    assert captured == {"file": stream, "all_threads": True}
    files = list(tmp_path.glob("runtime-native-*.log"))
    assert len(files) == 1
    path = files[0]
    assert path.parent == tmp_path
    assert path.name.startswith("runtime-native-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    descriptor = os.open(path, os.O_RDONLY)
    try:
        assert os.read(descriptor, 1) == b""
    finally:
        os.close(descriptor)
    stream.close()
