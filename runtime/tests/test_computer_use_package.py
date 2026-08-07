from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from shejane_runtime.plugins.computer_use import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_PLUGIN_VERSION,
    ComputerUseActionExecutor,
    ComputerUseError,
    ComputerUseReadiness,
    ComputerUseService,
    is_allowed_computer_use_package,
)
from shejane_runtime.plugins.manifest import load_plugin_manifest
from shejane_runtime.store.sqlite import LocalStore, PluginStateError

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "computer-use"


class FakeReadinessService:
    def __init__(self) -> None:
        self.installed = False
        self.ready = False
        self.accessibility = False
        self.screen_recording = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, action: str, arguments: dict[str, Any], *, timeout_ms: int
    ) -> dict[str, Any]:
        assert timeout_ms == 120_000
        self.calls.append((action, arguments))
        if action == "readiness.inspect":
            return {
                "installed": self.installed,
                "helper_ready": self.ready,
                "helper_identity_valid": self.ready,
                "accessibility": self.accessibility,
                "screen_recording": self.screen_recording,
            }
        if action == "readiness.install":
            self.installed = True
            self.ready = True
            return {}
        if action == "readiness.recheck":
            return {}
        if action in {"readiness.request_permission", "readiness.open_settings"}:
            return {}
        raise AssertionError(action)


@pytest.mark.asyncio
async def test_computer_use_readiness_advances_one_user_permission_at_a_time() -> None:
    service = FakeReadinessService()
    readiness = ComputerUseReadiness(service)  # type: ignore[arg-type]

    initial = await readiness.inspect(stage="idle", revision=0)
    assert initial == {
        "state": "action_required",
        "revision": 0,
        "step": "install_helper",
        "action_id": "install_helper",
        "can_recheck": False,
    }

    screen = await readiness.advance(action_id="install_helper", stage="idle", revision=0)
    assert screen["step"] == "screen_recording"
    assert screen["action_id"] == "request_screen_recording"

    awaiting_screen = await readiness.advance(
        action_id="request_screen_recording", stage="idle", revision=1
    )
    assert awaiting_screen == {
        "state": "awaiting_user",
        "revision": 2,
        "step": "screen_recording",
        "action_id": "open_screen_recording_settings",
        "can_recheck": True,
    }
    assert ("readiness.request_permission", {"kind": "screenRecording"}) in service.calls
    assert ("readiness.request_permission", {"kind": "accessibility"}) not in service.calls

    service.screen_recording = True
    accessibility = await readiness.advance(
        action_id="recheck", stage="screen_requested", revision=2
    )
    assert accessibility["step"] == "accessibility"
    assert accessibility["action_id"] == "request_accessibility"

    awaiting_accessibility = await readiness.advance(
        action_id="request_accessibility", stage="idle", revision=3
    )
    assert awaiting_accessibility["step"] == "accessibility"
    assert awaiting_accessibility["can_recheck"] is True
    assert ("readiness.request_permission", {"kind": "accessibility"}) in service.calls

    service.accessibility = True
    ready = await readiness.advance(
        action_id="recheck", stage="accessibility_requested", revision=4
    )
    assert ready == {
        "state": "ready",
        "revision": 5,
        "step": None,
        "action_id": None,
        "can_recheck": False,
    }


@pytest.mark.asyncio
async def test_computer_use_readiness_inspection_never_repeats_permission_requests() -> None:
    service = FakeReadinessService()
    service.installed = True
    service.ready = True
    readiness = ComputerUseReadiness(service)  # type: ignore[arg-type]

    first = await readiness.inspect(stage="screen_requested", revision=7)
    second = await readiness.inspect(stage="screen_requested", revision=7)

    assert first == second
    assert first["state"] == "awaiting_user"
    assert {action for action, _arguments in service.calls} == {"readiness.inspect"}


@pytest.mark.asyncio
async def test_computer_use_readiness_accepts_a_step_completed_outside_shejane() -> None:
    service = FakeReadinessService()
    service.installed = True
    service.ready = True
    service.screen_recording = True
    readiness = ComputerUseReadiness(service)  # type: ignore[arg-type]

    snapshot = await readiness.advance(
        action_id="request_screen_recording", stage="idle", revision=2
    )

    assert snapshot["revision"] == 3
    assert snapshot["step"] == "accessibility"
    assert ("readiness.request_permission", {"kind": "screenRecording"}) not in service.calls


@pytest.mark.asyncio
async def test_computer_use_setup_flow_rejects_stale_user_actions(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.sqlite3")
    try:
        assert await store.get_plugin_setup_flow(
            principal_id="local", plugin_id=COMPUTER_USE_PLUGIN_ID
        ) == {"stage": "idle", "revision": 0, "updated_at": None}

        advanced = await store.begin_plugin_setup_action(
            principal_id="local",
            plugin_id=COMPUTER_USE_PLUGIN_ID,
            expected_revision=0,
            next_stage="screen_requested",
        )
        assert advanced["stage"] == "screen_requested"
        assert advanced["revision"] == 1

        with pytest.raises(PluginStateError, match="plugin setup state changed"):
            await store.begin_plugin_setup_action(
                principal_id="local",
                plugin_id=COMPUTER_USE_PLUGIN_ID,
                expected_revision=0,
                next_stage="screen_requested",
            )
    finally:
        await store.close()


def test_computer_use_manifest_exposes_state_scoped_desktop_actions(tmp_path: Path) -> None:
    shutil.copytree(PLUGIN / "actions", tmp_path / "actions")
    shutil.copytree(PLUGIN / "commands", tmp_path / "commands")
    (tmp_path / ".shejane-plugin").mkdir()
    manifest = (PLUGIN / ".shejane-plugin" / "plugin.template.json").read_text()
    manifest = manifest.replace("__PLUGIN_VERSION__", "0.1.0").replace(
        "__PLATFORM__", "darwin/arm64"
    )
    (tmp_path / ".shejane-plugin" / "plugin.json").write_text(manifest)

    parsed = load_plugin_manifest(tmp_path)

    assert parsed.runtime.execution.kind == "builtin"
    assert parsed.runtime.execution.handler == "computer_use"
    assert [action.id for action in parsed.contributions.actions] == [
        "find_roots",
        "observe_ui",
        "search_ui",
        "expand_ui",
        "inspect_ui",
        "act_ui",
        "read_text",
        "wait_for",
    ]
    assert next(
        action for action in parsed.contributions.actions if action.id == "act_ui"
    ).capabilities == ["computer.control"]


def test_computer_use_builtin_requires_the_fixed_runtime_identity() -> None:
    identity = {
        "plugin_id": COMPUTER_USE_PLUGIN_ID,
        "version": COMPUTER_USE_PLUGIN_VERSION,
        "handler": "computer_use",
    }
    assert is_allowed_computer_use_package(**identity)
    assert not is_allowed_computer_use_package(**{**identity, "version": "9.9.9"})


def test_computer_use_service_exposes_the_frozen_helper_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    helper = tmp_path / "computer-use-helper" / "pi-computer-use.app"
    helper.mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    service = ComputerUseService(package, workspace_root=tmp_path)

    assert service._extra_environment()["SHEJANE_COMPUTER_USE_HELPER_APP"] == str(helper)


@pytest.mark.asyncio
async def test_computer_use_executor_preserves_plugin_result_identity(tmp_path: Path) -> None:
    class FakeService:
        async def call(
            self, action: str, arguments: dict[str, Any], *, timeout_ms: int
        ) -> dict[str, Any]:
            assert (action, arguments, timeout_ms) == ("status", {}, 15_000)
            return {"text": "ready", "details": {"accessibility": True}}

    executor = ComputerUseActionExecutor(FakeService(), "status")  # type: ignore[arg-type]
    result = await executor.invoke(
        {
            "invocation_id": "invocation-1",
            "operation_id": "operation-1",
            "arguments": {},
            "limits": {"timeout_ms": 15_000},
        },
        input_root=tmp_path,
        output_root=tmp_path,
    )

    assert result == {
        "schema_version": 1,
        "invocation_id": "invocation-1",
        "operation_id": "operation-1",
        "status": "succeeded",
        "output": {"text": "ready", "details": {"accessibility": True}},
        "artifacts": [],
    }


@pytest.mark.asyncio
async def test_computer_use_service_rejects_oversized_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    bridge = package / "payload" / "bridge-server.mjs"
    bridge.parent.mkdir(parents=True)
    bridge.write_text(
        "import sys\n"
        "sys.stdin.readline()\n"
        'sys.stdout.write(\'{"id":1,"result":{"text":"\' + \'x\' * 128 + \'"}}\\n\')\n'
        "sys.stdout.flush()\n"
    )
    monkeypatch.setenv("SHEJANE_RUNTIME_NODE_PATH", sys.executable)
    monkeypatch.setattr("shejane_runtime.plugins.computer_use.MAX_FRAME_BYTES", 64)
    service = ComputerUseService(package, workspace_root=tmp_path)

    try:
        with pytest.raises(ComputerUseError, match="response exceeds the protocol limit"):
            await service.call("status", {}, timeout_ms=1_000)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_existing_plugin_table_migrates_to_builtin_execution(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE plugin_versions ("
            "plugin_id TEXT NOT NULL, version TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, "
            "manifest_json TEXT NOT NULL, execution_kind TEXT NOT NULL "
            "CHECK (execution_kind IN ('wasi', 'managed_worker')), "
            "signature_status TEXT NOT NULL CHECK (signature_status IN ('unsigned', 'verified')), "
            "signer_key_id TEXT, compatibility TEXT NOT NULL "
            "CHECK (compatibility IN ('compatible', 'incompatible')), source TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK (state IN ('installed', 'retired')), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, retired_at TEXT, "
            "PRIMARY KEY (plugin_id, digest), UNIQUE (plugin_id, version))"
        )
        connection.execute(
            "INSERT INTO plugin_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "dev.shejane.fixture.archive",
                "0.1.0",
                "sha256:" + "a" * 64,
                "{}",
                "wasi",
                "unsigned",
                None,
                "compatible",
                "local_file",
                "installed",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
        connection.execute(
            "CREATE TABLE plugin_installations ("
            "principal_id TEXT NOT NULL, plugin_id TEXT NOT NULL, active_digest TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)), source TEXT NOT NULL, "
            "revision INTEGER NOT NULL DEFAULT 1, model_binding_json TEXT, "
            "model_binding_revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, retired_at TEXT, PRIMARY KEY (principal_id, plugin_id), "
            "FOREIGN KEY (active_digest) REFERENCES plugin_versions(digest))"
        )
        connection.execute(
            "INSERT INTO plugin_installations VALUES (?, ?, ?, 1, ?, 1, NULL, 0, ?, ?, NULL)",
            (
                "local:owner",
                "dev.shejane.fixture.archive",
                "sha256:" + "a" * 64,
                "local_file",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )

    store = await LocalStore.open(database)
    await store.close()

    with sqlite3.connect(database) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'plugin_versions'"
        ).fetchone()[0]
        row = connection.execute("SELECT plugin_id, execution_kind FROM plugin_versions").fetchone()
        active = connection.execute(
            "SELECT plugin_id, enabled FROM plugin_installations"
        ).fetchone()
    assert "'builtin'" in schema
    assert row == ("dev.shejane.fixture.archive", "wasi")
    assert active == ("dev.shejane.fixture.archive", 1)
