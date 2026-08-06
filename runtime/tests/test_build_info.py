from __future__ import annotations

from shejane_runtime.build_info import runtime_build_identity


def test_runtime_build_identity_uses_launcher_supplied_release_values(monkeypatch) -> None:
    monkeypatch.setenv("SHEJANE_CLIENT_RELEASE", "0.2.0")
    monkeypatch.setenv("SHEJANE_RUNTIME_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("SHEJANE_RUNTIME_BUILD_ID", "build-7")
    monkeypatch.setenv("SHEJANE_RUNTIME_PACKAGING_MODE", "frozen")

    identity = runtime_build_identity(protocol_version=1)

    assert identity["client_release"] == "0.2.0"
    assert identity["build_commit"] == "abc123"
    assert identity["build_id"] == "build-7"
    assert identity["packaging_mode"] == "frozen"
    assert identity["platform"]
    assert identity["arch"]
