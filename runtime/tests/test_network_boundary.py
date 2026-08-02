from __future__ import annotations

import pytest
from pydantic import ValidationError

from shejane_runtime.config import Settings


def test_runtime_listener_accepts_only_explicit_loopback_hosts() -> None:
    assert Settings(SHEJANE_RUNTIME_HOST="localhost").host == "localhost"
    assert Settings(SHEJANE_RUNTIME_HOST="127.0.0.2").host == "127.0.0.2"
    assert Settings(SHEJANE_RUNTIME_HOST="::1").host == "::1"

    for unsafe_host in ("0.0.0.0", "::", "192.168.1.10", "runtime.example.com"):
        with pytest.raises(ValidationError, match="loopback"):
            Settings(SHEJANE_RUNTIME_HOST=unsafe_host)


def test_fixed_runtime_asset_source_requires_credential_free_https() -> None:
    settings = Settings(
        fixed_runtime_asset_base_url="https://downloads.example.test/client-v0.1.27/"
    )
    assert settings.fixed_runtime_asset_base_url == (
        "https://downloads.example.test/client-v0.1.27"
    )

    for unsafe_url in (
        "http://downloads.example.test/client-v0.1.27",
        "https://user:secret@downloads.example.test/client-v0.1.27",
        "https://downloads.example.test/client-v0.1.27?token=secret",
    ):
        with pytest.raises(ValidationError, match="credential-free HTTPS"):
            Settings(fixed_runtime_asset_base_url=unsafe_url)
