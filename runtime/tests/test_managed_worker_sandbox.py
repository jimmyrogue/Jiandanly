from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from shejane_runtime.plugins.executor import ManagedWorkerActionExecutor
from shejane_runtime.plugins.linux_cgroup import LinuxCgroupResources
from shejane_runtime.plugins.managed_worker import _create_vm_staging, _reap_stale_vm_staging
from shejane_runtime.plugins.runtime_assets import RuntimeAssetHandle
from shejane_runtime.plugins.sandbox_runtime import (
    SandboxProcessIdentity,
    SandboxRuntimeError,
    configured_srt_launcher,
    managed_worker_release_gate,
    prepare_agent_shell_command,
    prepare_srt_command,
    read_process_command,
    read_process_start,
    sandbox_process_matches,
    terminate_sandbox_process,
)
from shejane_runtime.plugins.tools import PluginActionError, _executor_for_action

REPO_ROOT = Path(__file__).resolve().parents[2]
SRT_CLI = (
    REPO_ROOT
    / "apps"
    / "desktop"
    / "node_modules"
    / "@anthropic-ai"
    / "sandbox-runtime"
    / "dist"
    / "cli.js"
)


def test_vm_staging_lease_survives_runtime_exit_until_launcher_exits(
    tmp_path: Path,
) -> None:
    staging, lease_fd = _create_vm_staging(tmp_path)
    (staging / "scratch.ext4").write_bytes(b"staged")
    launcher = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
        pass_fds=(lease_fd,),
    )
    os.close(lease_fd)
    try:
        _reap_stale_vm_staging(tmp_path)
        assert staging.is_dir()
        assert launcher.wait(timeout=2) == 0
        _reap_stale_vm_staging(tmp_path)
        assert not staging.exists()
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=2)


def test_vm_staging_reaper_ignores_unowned_shapes(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "vm-invocation-symlink"
    symlink.symlink_to(outside, target_is_directory=True)
    missing_lease = tmp_path / "vm-invocation-missing-lease"
    missing_lease.mkdir(mode=0o700)

    _reap_stale_vm_staging(tmp_path)

    assert symlink.is_symlink()
    assert outside.is_dir()
    assert missing_lease.is_dir()


def test_srt_policy_denies_root_and_grants_only_package_input_output(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    package_root.mkdir()
    input_root.mkdir()
    output_root.mkdir()
    runtime_asset_root = tmp_path / "runtime-asset"
    runtime_asset_root.mkdir()
    entrypoint = package_root / "worker"
    entrypoint.write_bytes(b"worker")
    launcher = tmp_path / "srt"
    launcher.write_bytes(b"launcher")

    command = prepare_srt_command(
        launcher=(str(launcher),),
        worker_command=[str(entrypoint)],
        package_root=package_root,
        input_root=input_root,
        output_root=output_root,
        runtime_asset_roots=(runtime_asset_root,),
    )

    settings_path = tmp_path / "sandbox-settings.json"
    policy = json.loads(settings_path.read_text())
    assert command == [str(launcher), "-s", str(settings_path), str(entrypoint)]
    assert policy["filesystem"]["denyRead"] == [package_root.anchor]
    assert str(package_root) in policy["filesystem"]["allowRead"]
    assert str(input_root) in policy["filesystem"]["allowRead"]
    assert str(runtime_asset_root) in policy["filesystem"]["allowRead"]
    assert policy["filesystem"]["allowWrite"] == [str(output_root)]
    assert policy["network"]["allowedDomains"] == []
    assert policy["network"]["allowAllUnixSockets"] is False
    assert policy["enableWeakerNestedSandbox"] is False
    assert policy["enableWeakerNetworkIsolation"] is False


def test_agent_shell_policy_is_read_only_outside_private_scratch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    executable_root = tmp_path / "bin"
    workspace.mkdir()
    scratch.mkdir()
    executable_root.mkdir()
    launcher = tmp_path / "srt"
    launcher.write_bytes(b"launcher")

    command = prepare_agent_shell_command(
        launcher=(str(launcher),),
        command="make test",
        workspace_root=workspace,
        scratch_root=scratch,
        executable_roots=(executable_root,),
    )

    settings_path = scratch / "sandbox-settings.json"
    policy = json.loads(settings_path.read_text())
    assert command == [str(launcher), "-s", str(settings_path), "-c", "make test"]
    assert policy["filesystem"]["denyRead"] == [workspace.anchor]
    assert str(workspace) in policy["filesystem"]["allowRead"]
    assert str(executable_root) in policy["filesystem"]["allowRead"]
    assert policy["filesystem"]["allowWrite"] == [str(scratch)]
    assert str(workspace) not in policy["filesystem"]["allowWrite"]
    assert policy["network"]["allowedDomains"] == []
    assert policy["network"]["allowLocalBinding"] is False
    assert policy["network"]["allowAllUnixSockets"] is False
    assert policy["allowAppleEvents"] is False


@contextmanager
def _sandbox_stand_in(marker: str) -> Iterator[subprocess.Popen[bytes]]:
    """A session-leading process tree shaped like a real sandbox launcher.

    ``& wait`` keeps the outer shell alive with the marker in its argv, so the
    tree has a supervisor and a child exactly like launcher-over-command.
    """

    process = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & wait", marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        with suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def _identity_for(process: subprocess.Popen[bytes], marker: str) -> SandboxProcessIdentity:
    started_at = read_process_start(process.pid)
    assert started_at is not None
    return SandboxProcessIdentity(pid=process.pid, started_at=started_at, settings_path=marker)


def test_sandbox_identity_matches_the_process_it_recorded() -> None:
    marker = f"shejane-identity-{uuid.uuid4().hex}"
    with _sandbox_stand_in(marker) as process:
        identity = _identity_for(process, marker)
        assert marker in (read_process_command(process.pid) or "")
        assert sandbox_process_matches(identity) is True


def test_sandbox_identity_rejects_a_recycled_pid() -> None:
    """A pid whose start time moved on is a different process wearing the number."""

    marker = f"shejane-identity-{uuid.uuid4().hex}"
    with _sandbox_stand_in(marker) as process:
        recorded = _identity_for(process, marker)
        recycled = SandboxProcessIdentity(
            pid=recorded.pid,
            started_at=recorded.started_at + "9",
            settings_path=recorded.settings_path,
        )
        assert sandbox_process_matches(recycled) is False


def test_sandbox_identity_rejects_an_unrelated_command() -> None:
    marker = f"shejane-identity-{uuid.uuid4().hex}"
    with _sandbox_stand_in(marker) as process:
        recorded = _identity_for(process, marker)
        impostor = SandboxProcessIdentity(
            pid=recorded.pid,
            started_at=recorded.started_at,
            settings_path=f"shejane-identity-{uuid.uuid4().hex}",
        )
        assert sandbox_process_matches(impostor) is False


def test_terminating_a_sandbox_process_takes_the_whole_group() -> None:
    marker = f"shejane-identity-{uuid.uuid4().hex}"
    with _sandbox_stand_in(marker) as process:
        children = subprocess.run(
            ["pgrep", "-P", str(process.pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        descendants = [int(value) for value in children.stdout.split() if value.isdigit()]
        assert descendants, "stand-in never spawned the wrapped command"

        assert terminate_sandbox_process(_identity_for(process, marker)) == "reaped"

        assert read_process_start(process.pid) is None
        for descendant in descendants:
            assert read_process_start(descendant) is None, (
                f"descendant {descendant} outlived the group kill"
            )


def test_terminating_refuses_a_stale_record_instead_of_killing_a_bystander() -> None:
    """The guard that keeps a recycled pid from costing an unrelated process."""

    marker = f"shejane-identity-{uuid.uuid4().hex}"
    with _sandbox_stand_in(marker) as process:
        recorded = _identity_for(process, marker)
        stale = SandboxProcessIdentity(
            pid=recorded.pid,
            started_at=recorded.started_at + "9",
            settings_path=recorded.settings_path,
        )

        assert terminate_sandbox_process(stale) == "stale"
        assert read_process_start(process.pid) is not None, "bystander was signalled"


def test_terminating_reports_an_already_finished_process_as_gone() -> None:
    marker = f"shejane-identity-{uuid.uuid4().hex}"
    with _sandbox_stand_in(marker) as process:
        recorded = _identity_for(process, marker)
    # Leaving the block killed the tree, so the record now names nothing.
    assert terminate_sandbox_process(recorded) == "gone"


def _write_seccomp_launcher(root: Path, architecture: str = "x64") -> tuple[str, ...]:
    """Lay out a node + sandbox-runtime launcher that ships an apply-seccomp helper."""

    node = root / "node"
    node.write_bytes(b"node")
    package = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    cli = package / "dist" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("cli")
    helper = package / "vendor" / "seccomp" / architecture / "apply-seccomp"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"apply-seccomp")
    return (str(node), str(cli))


def test_agent_shell_policy_allows_the_launcher_seccomp_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    launcher = _write_seccomp_launcher(tmp_path)

    prepare_agent_shell_command(
        launcher=launcher,
        command="make test",
        workspace_root=workspace,
        scratch_root=scratch,
    )

    policy = json.loads((scratch / "sandbox-settings.json").read_text())
    helper_root = Path(launcher[1]).parents[1] / "vendor" / "seccomp" / "x64"
    assert str(helper_root.resolve()) in policy["filesystem"]["allowRead"]


def test_srt_policy_allows_the_launcher_seccomp_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    package_root = tmp_path / "package"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    package_root.mkdir()
    input_root.mkdir()
    output_root.mkdir()
    entrypoint = package_root / "worker"
    entrypoint.write_bytes(b"worker")
    launcher = _write_seccomp_launcher(tmp_path)

    prepare_srt_command(
        launcher=launcher,
        worker_command=[str(entrypoint)],
        package_root=package_root,
        input_root=input_root,
        output_root=output_root,
    )

    policy = json.loads((tmp_path / "sandbox-settings.json").read_text())
    helper_root = Path(launcher[1]).parents[1] / "vendor" / "seccomp" / "x64"
    assert str(helper_root.resolve()) in policy["filesystem"]["allowRead"]


def test_agent_shell_policy_keeps_the_seccomp_helper_off_non_linux_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    launcher = _write_seccomp_launcher(tmp_path)

    prepare_agent_shell_command(
        launcher=launcher,
        command="make test",
        workspace_root=workspace,
        scratch_root=scratch,
    )

    policy = json.loads((scratch / "sandbox-settings.json").read_text())
    assert not any(
        "apply-seccomp" in path or "vendor" in path for path in policy["filesystem"]["allowRead"]
    )


def test_srt_policy_rejects_worker_outside_package(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    package_root.mkdir()
    input_root.mkdir()
    output_root.mkdir()
    entrypoint = tmp_path / "worker"
    entrypoint.write_bytes(b"worker")
    launcher = tmp_path / "srt"
    launcher.write_bytes(b"launcher")

    with pytest.raises(SandboxRuntimeError, match="outside the package"):
        prepare_srt_command(
            launcher=(str(launcher),),
            worker_command=[str(entrypoint)],
            package_root=package_root,
            input_root=input_root,
            output_root=output_root,
        )


def test_srt_policy_rejects_overlapping_roots(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    output_root = package_root / "output"
    package_root.mkdir()
    output_root.mkdir()
    entrypoint = package_root / "worker"
    entrypoint.write_bytes(b"worker")
    launcher = tmp_path / "srt"
    launcher.write_bytes(b"launcher")

    with pytest.raises(SandboxRuntimeError, match="must not overlap"):
        prepare_srt_command(
            launcher=(str(launcher),),
            worker_command=[str(entrypoint)],
            package_root=package_root,
            input_root=tmp_path,
            output_root=output_root,
        )


def test_configured_srt_launcher_requires_absolute_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    monkeypatch.setenv(
        "SHEJANE_MANAGED_WORKER_SANDBOX_COMMAND",
        json.dumps([str(launcher), "/resources/srt-launcher.mjs"]),
    )
    assert configured_srt_launcher() == (str(launcher), "/resources/srt-launcher.mjs")

    monkeypatch.setenv("SHEJANE_MANAGED_WORKER_SANDBOX_COMMAND", '["relative"]')
    with pytest.raises(SandboxRuntimeError, match="executable is unavailable"):
        configured_srt_launcher()


def test_managed_worker_release_gate_is_explicit_and_fail_closed() -> None:
    darwin = managed_worker_release_gate("darwin/arm64")
    darwin_intel = managed_worker_release_gate("darwin/amd64")
    linux = managed_worker_release_gate("linux/arm64")
    linux_amd64 = managed_worker_release_gate("linux/amd64")
    windows = managed_worker_release_gate("windows/amd64")

    assert darwin.adapter_id == "darwin_vf_linux_vm_v1"
    assert darwin.proved == (
        "cooperative_guest_shutdown",
        "deterministic_ext4_disk_images",
        "deterministic_minimal_guest_boot",
        "dynamic_node_worker_runtime",
        "dynamic_python_worker_runtime",
        "fixed_capacity_scratch_mount",
        "frozen_read_only_guest_rootfs",
        "frozen_vm_asset_set",
        "guest_cancel_process_tree_cleanup",
        "guest_cgroup_v2_resource_policy",
        "guest_host_protocol",
        "hard_cpu_memory_process_tree_limits",
        "host_file_credential_network_ipc_isolation",
        "input_output_disk_limits",
        "invocation_private_noexec_tmp_mount",
        "launcher_crash_cleanup",
        "nonprivileged_guest_worker_action_protocol",
        "production_asset_manifest_preflight",
        "read_only_input_mount",
        "read_only_package_mount",
        "runtime_adapter_vm_roundtrip",
        "runtime_crash_lease_recovery",
        "virtio_socket_handshake",
        "virtualization_framework_boot",
        "vsock_artifact_extraction",
        "worker_crash_vm_cleanup",
    )
    assert darwin.blockers == ("packaged_backend_absent", "release_ci_gate")
    assert darwin.enabled is False

    assert darwin_intel.adapter_id == "darwin_vf_linux_vm_v1"
    assert darwin_intel.proved == ()
    assert darwin_intel.blockers == ("architecture_conformance_gate",)
    assert darwin_intel.enabled is False

    assert linux.adapter_id == "linux_bwrap_cgroup_v1"
    assert linux.proved == (
        "artifact_broker_declared_output_only",
        "bubblewrap_namespaces",
        "cgroup_v2_resource_limits",
        "descendant_access_isolation",
        "fixed_capacity_private_scratch",
        "read_only_package_input_rootfs",
        "seccomp_network_and_escape_filter",
        "worker_tree_cancel_cleanup",
    )
    assert linux.blockers == ("systemd_delegation_gate", "release_ci_gate")
    assert linux.enabled is False

    assert linux_amd64.adapter_id == "linux_bwrap_cgroup_v1"
    assert linux_amd64.proved == ()
    assert linux_amd64.blockers == (
        "architecture_conformance_gate",
        "systemd_delegation_gate",
        "release_ci_gate",
    )
    assert linux_amd64.enabled is False

    assert windows.proved == ()
    assert windows.adapter_id == "windows_qemu_linux_vm_v1"
    assert windows.blockers == (
        "appcontainer_lpac_vmm_isolation",
        "architecture_conformance_gate",
        "descendant_escape_and_cleanup",
        "fixed_capacity_guest_scratch",
        "guest_cgroup_v2_resource_policy",
        "host_job_object_resource_limits",
        "no_network_guest",
        "packaged_launcher",
        "qemu_supply_chain",
        "read_only_package_input_media",
        "release_ci_gate",
    )
    assert windows.enabled is False


def test_managed_worker_release_gate_rejects_unknown_platform() -> None:
    gate = managed_worker_release_gate("plan9/mips")

    assert gate.adapter_id is None
    assert gate.proved == ()
    assert gate.blockers == ("unsupported_platform",)
    assert gate.enabled is False


def test_default_managed_worker_executor_cannot_bypass_release_gate() -> None:
    with pytest.raises(PluginActionError, match="release gate is closed"):
        _executor_for_action(SimpleNamespace(execution_kind="managed_worker"))  # type: ignore[arg-type]


def test_darwin_managed_worker_cannot_fall_back_to_srt_without_vm_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shejane_runtime.plugins.tools as tools_module

    action = SimpleNamespace(
        execution_kind="managed_worker",
        entrypoint=tmp_path / "package" / "worker",
        package_root=tmp_path / "package",
        runtime_assets=(),
    )
    monkeypatch.setattr(tools_module, "current_managed_worker_platform", lambda: "darwin/arm64")
    monkeypatch.setattr(
        tools_module,
        "managed_worker_release_gate",
        lambda _platform: SimpleNamespace(enabled=True, blockers=()),
    )

    with pytest.raises(PluginActionError, match="requires packaged VM resources"):
        _executor_for_action(action)  # type: ignore[arg-type]


def test_linux_managed_worker_uses_the_native_executor_after_its_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shejane_runtime.plugins.tools as tools_module

    resources = LinuxCgroupResources(
        launcher=tmp_path / "launcher",
        delegated_root=tmp_path / "cgroup",
        bubblewrap=tmp_path / "bwrap",
    )
    action = SimpleNamespace(
        execution_kind="managed_worker",
        entrypoint=tmp_path / "package" / "worker",
        package_root=tmp_path / "package",
        runtime_assets=(),
    )
    monkeypatch.setattr(tools_module, "current_managed_worker_platform", lambda: "linux/arm64")
    monkeypatch.setattr(
        tools_module,
        "managed_worker_release_gate",
        lambda _platform: SimpleNamespace(enabled=True, blockers=()),
    )

    executor = _executor_for_action(action, linux_cgroup=resources)  # type: ignore[arg-type]

    assert isinstance(executor, ManagedWorkerActionExecutor)
    assert executor.linux_cgroup is resources


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt conformance")
async def test_srt_blocks_host_access_from_worker_descendant(tmp_path: Path) -> None:
    launcher_executable = os.environ.get("SHEJANE_TEST_SRT_EXECUTABLE") or shutil.which("node")
    launcher_cli = os.environ.get("SHEJANE_TEST_SRT_CLI") or str(SRT_CLI)
    cli_available = Path(launcher_cli).is_file() or "app.asar/" in launcher_cli
    if not launcher_executable or not cli_available or not Path("/usr/bin/cc").is_file():
        pytest.skip("pinned SRT or C compiler unavailable")

    package_root = tmp_path / "package"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    package_root.mkdir()
    input_root.mkdir()
    output_root.mkdir()
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text("host secret")
    unix_path = Path("/tmp") / f"shejane-srt-{uuid.uuid4().hex}.sock"

    try:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_server,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix_server,
        ):
            tcp_server.bind(("127.0.0.1", 0))
            tcp_server.listen()
            tcp_port = int(tcp_server.getsockname()[1])
            unix_server.bind(str(unix_path))
            unix_server.listen()
            source = package_root / "worker.c"
            worker = package_root / "worker"
            source.write_text(
                _probe_worker_source(
                    forbidden=forbidden,
                    tcp_port=tcp_port,
                    unix_path=unix_path,
                    host_pid=os.getpid(),
                )
            )
            compiler = await asyncio.create_subprocess_exec(
                "/usr/bin/cc", str(source), "-o", str(worker)
            )
            assert await compiler.wait() == 0

            result = await ManagedWorkerActionExecutor(
                (str(worker),),
                sandbox_command=(launcher_executable, launcher_cli),
                package_root=package_root,
            ).invoke(
                {
                    "invocation_id": "523e4567-e89b-42d3-a456-426614174004",
                    "operation_id": "run_01:sandbox.probe:001",
                    "action": {
                        "plugin_id": "dev.shejane.fixture.sandbox",
                        "plugin_digest": "sha256:" + "d" * 64,
                        "action_id": "sandbox.probe",
                    },
                    "grants": {"capabilities": []},
                    "limits": {"timeout_ms": 5_000, "memory_mb": 64, "output_mb": 1},
                },
                input_root=input_root,
                output_root=output_root,
            )
    finally:
        unix_path.unlink(missing_ok=True)

    assert result["status"] == "succeeded"
    assert result["output"] == {"sandbox_enforced": True}


@pytest.mark.asyncio
async def test_managed_worker_receives_exact_runtime_asset_mapping(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    asset_root = tmp_path / "asset"
    input_root.mkdir()
    output_root.mkdir()
    asset_root.mkdir()
    marker = asset_root / "engine.bin"
    marker.write_text("pinned")
    script = r"""
import json, os, sys
initialize = json.loads(sys.stdin.readline())
assets = json.loads(os.environ["SHEJANE_PLUGIN_RUNTIME_ASSETS"])
declared = initialize["params"]["runtime_assets"]
valid = declared == [{"id":"org.libreoffice.runtime","version":"25.8.7","digest":"sha256:" + "a" * 64}] and open(assets["org.libreoffice.runtime"] + "/engine.bin").read() == "pinned"
print(json.dumps({"jsonrpc":"2.0","id":1,"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}}), flush=True)
invoke = json.loads(sys.stdin.readline())
params = invoke["params"]
print(json.dumps({"jsonrpc":"2.0","id":2,"result":{"invocation_id":params["invocation_id"],"operation_id":params["operation_id"],"status":"succeeded","output":{"valid":valid},"artifacts":[]}}), flush=True)
json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0","id":3,"result":{}}), flush=True)
"""
    asset = RuntimeAssetHandle(
        asset_id="org.libreoffice.runtime",
        version="25.8.7",
        platform="darwin/arm64",
        digest="sha256:" + "a" * 64,
        root=asset_root,
        payload=asset_root,
        license="MPL-2.0",
        source_url="https://www.libreoffice.org/",
        sbom=asset_root / "sbom.spdx.json",
    )

    result = await ManagedWorkerActionExecutor(
        (sys.executable, "-c", script),
        runtime_assets=(asset,),
    ).invoke(
        {
            "invocation_id": "623e4567-e89b-42d3-a456-426614174005",
            "operation_id": "run_01:asset.probe:001",
            "action": {
                "plugin_id": "dev.shejane.fixture.asset",
                "plugin_digest": "sha256:" + "d" * 64,
                "action_id": "asset.probe",
            },
            "grants": {"capabilities": []},
            "limits": {"timeout_ms": 5_000, "memory_mb": 64, "output_mb": 1},
        },
        input_root=input_root,
        output_root=output_root,
    )

    assert result["output"] == {"valid": True}


def _probe_worker_source(*, forbidden: Path, tcp_port: int, unix_path: Path, host_pid: int) -> str:
    forbidden_literal = json.dumps(str(forbidden))
    unix_literal = json.dumps(str(unix_path))
    return rf"""
#include <arpa/inet.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {{
  char line[8192];
  if (!fgets(line, sizeof(line), stdin)) return 1;
  puts("{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{\"protocol_version\":1,\"process_isolated\":true,\"access_isolated\":true,\"resource_isolated\":false,\"sandboxed\":false}}}}");
  fflush(stdout);
  if (!fgets(line, sizeof(line), stdin)) return 2;

  pid_t child = fork();
  if (child == 0) {{
    int leaked = 0;
    int fd = open({forbidden_literal}, O_RDONLY);
    if (fd >= 0) {{ leaked |= 1; close(fd); }}

    int tcp = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons({tcp_port});
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
    if (tcp >= 0 && connect(tcp, (struct sockaddr *)&address, sizeof(address)) == 0) leaked |= 2;
    if (tcp >= 0) close(tcp);

    if (kill({host_pid}, 0) == 0) leaked |= 4;

    int local = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un local_address;
    memset(&local_address, 0, sizeof(local_address));
    local_address.sun_family = AF_UNIX;
    strncpy(local_address.sun_path, {unix_literal}, sizeof(local_address.sun_path) - 1);
    if (local >= 0 && connect(local, (struct sockaddr *)&local_address, sizeof(local_address)) == 0) leaked |= 8;
    if (local >= 0) close(local);
    _exit(leaked);
  }}

  int status = 0;
  waitpid(child, &status, 0);
  int enforced = WIFEXITED(status) && WEXITSTATUS(status) == 0;
  printf("{{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{{\"schema_version\":1,\"invocation_id\":\"523e4567-e89b-42d3-a456-426614174004\",\"operation_id\":\"run_01:sandbox.probe:001\",\"status\":\"succeeded\",\"output\":{{\"sandbox_enforced\":%s}},\"artifacts\":[]}}}}\n", enforced ? "true" : "false");
  fflush(stdout);
  if (!fgets(line, sizeof(line), stdin)) return 3;
  puts("{{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{{}}}}");
  fflush(stdout);
  return 0;
}}
"""
