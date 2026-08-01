from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shejane_runtime import config


def test_frozen_windows_runtime_discovers_fixed_browser_qa_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "builtin-plugins" / "browser-qa-0.1.2-windows-amd64.shejane-plugin"
    asset = (
        tmp_path
        / "builtin-assets"
        / "browser-qa-runtime-1.61.1-windows-amd64.shejane-runtime-asset"
    )
    plugin.parent.mkdir()
    asset.parent.mkdir()
    plugin.write_bytes(b"plugin")
    asset.write_bytes(b"asset")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setattr(config.platform, "machine", lambda: "AMD64")

    assert config.default_browser_qa_package() == plugin
    assert config.default_browser_qa_runtime_asset() == asset
