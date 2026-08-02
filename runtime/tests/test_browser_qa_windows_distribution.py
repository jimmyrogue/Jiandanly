from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shejane_runtime import config


def test_frozen_windows_runtime_discovers_only_browser_qa_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "builtin-plugins" / "browser-qa-0.1.3-windows-amd64.shejane-plugin"
    plugin.parent.mkdir()
    plugin.write_bytes(b"plugin")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setattr(config.platform, "machine", lambda: "AMD64")

    assert config.default_browser_qa_package() == plugin
    assert config.Settings().browser_qa_runtime_asset is None
