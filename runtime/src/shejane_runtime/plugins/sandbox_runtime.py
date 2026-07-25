"""Strict Anthropic Sandbox Runtime policy for Managed Workers."""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path


class SandboxRuntimeError(RuntimeError):
    """The configured OS sandbox cannot safely launch this worker."""


@dataclass(frozen=True)
class ManagedWorkerReleaseGate:
    """Auditable platform evidence required before Registry enablement."""

    target_platform: str
    adapter_id: str | None
    proved: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def enabled(self) -> bool:
        return bool(self.proved) and not self.blockers


def managed_worker_release_gate(target_platform: str) -> ManagedWorkerReleaseGate:
    """Return the immutable, fail-closed release state for one target platform."""

    if target_platform == "darwin/arm64":
        return ManagedWorkerReleaseGate(
            target_platform=target_platform,
            adapter_id="darwin_vf_linux_vm_v1",
            proved=(
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
                "packaged_desktop_runtime_entry",
                "packaged_launcher_entitlement",
                "packaged_launcher_vm_transport",
                "packaged_vm_asset_set",
                "production_asset_manifest_preflight",
                "read_only_input_mount",
                "read_only_package_mount",
                "runtime_adapter_vm_roundtrip",
                "runtime_crash_lease_recovery",
                "virtio_socket_handshake",
                "virtualization_framework_boot",
                "vsock_artifact_extraction",
                "worker_crash_vm_cleanup",
            ),
            blockers=("release_ci_gate",),
        )
    if target_platform == "darwin/amd64":
        return ManagedWorkerReleaseGate(
            target_platform=target_platform,
            adapter_id="darwin_vf_linux_vm_v1",
            proved=(),
            blockers=("architecture_conformance_gate",),
        )
    if target_platform == "linux/arm64":
        return ManagedWorkerReleaseGate(
            target_platform=target_platform,
            adapter_id="linux_bwrap_cgroup_v1",
            proved=(
                "artifact_broker_declared_output_only",
                "bubblewrap_namespaces",
                "cgroup_v2_resource_limits",
                "descendant_access_isolation",
                "fixed_capacity_private_scratch",
                "read_only_package_input_rootfs",
                "seccomp_network_and_escape_filter",
                "worker_tree_cancel_cleanup",
            ),
            blockers=("systemd_delegation_gate", "release_ci_gate"),
        )
    if target_platform == "linux/amd64":
        return ManagedWorkerReleaseGate(
            target_platform=target_platform,
            adapter_id="linux_bwrap_cgroup_v1",
            proved=(),
            blockers=(
                "architecture_conformance_gate",
                "systemd_delegation_gate",
                "release_ci_gate",
            ),
        )
    if target_platform.startswith("windows/"):
        return ManagedWorkerReleaseGate(
            target_platform=target_platform,
            adapter_id="windows_qemu_linux_vm_v1",
            proved=(),
            blockers=(
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
            ),
        )
    return ManagedWorkerReleaseGate(
        target_platform=target_platform,
        adapter_id=None,
        proved=(),
        blockers=("unsupported_platform",),
    )


def configured_srt_launcher() -> tuple[str, ...] | None:
    raw = os.environ.get("SHEJANE_MANAGED_WORKER_SANDBOX_COMMAND")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SandboxRuntimeError("managed worker sandbox command is invalid") from exc
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 4
        or any(not isinstance(part, str) or not part for part in value)
    ):
        raise SandboxRuntimeError("managed worker sandbox command is invalid")
    executable = Path(value[0])
    if not executable.is_absolute() or not executable.is_file():
        raise SandboxRuntimeError("managed worker sandbox executable is unavailable")
    return tuple(value)


_SECCOMP_ARCHITECTURES = {
    "aarch64": "arm64",
    "amd64": "x64",
    "arm64": "arm64",
    "x86_64": "x64",
}


def _seccomp_helper_roots(launcher: tuple[str, ...]) -> tuple[Path, ...]:
    """Return the launcher's ``apply-seccomp`` directory, when it ships one.

    On Linux the launcher re-executes ``apply-seccomp`` from its own package
    *inside* the bwrap namespace, and that namespace only contains what
    ``allowRead`` binds back over the deny-read tmpfs. The helper lives under
    the launcher's package rather than a system prefix, so without this the
    wrapped command dies with ENOENT before it ever runs.
    """

    if not sys.platform.startswith("linux"):
        return ()
    architecture = _SECCOMP_ARCHITECTURES.get(platform.machine().lower())
    if architecture is None:
        return ()
    for part in launcher:
        candidate = Path(part)
        if not candidate.is_absolute():
            continue
        for parent in candidate.parents:
            helper_root = parent / "vendor" / "seccomp" / architecture
            if (helper_root / "apply-seccomp").is_file():
                return (helper_root.resolve(strict=True),)
    return ()


def prepare_srt_command(
    *,
    launcher: tuple[str, ...],
    worker_command: list[str],
    package_root: Path,
    input_root: Path,
    output_root: Path,
    runtime_asset_roots: tuple[Path, ...] = (),
) -> list[str]:
    """Write one deny-by-default SRT policy and return the wrapped command."""

    if not launcher or not worker_command:
        raise SandboxRuntimeError("sandbox launcher and worker command are required")
    launcher_path = Path(launcher[0])
    if not launcher_path.is_absolute() or not launcher_path.is_file():
        raise SandboxRuntimeError("sandbox launcher must be an absolute file")

    package_root = package_root.resolve(strict=True)
    input_root = input_root.resolve(strict=True)
    output_root = output_root.resolve(strict=True)
    asset_roots = tuple(path.resolve(strict=True) for path in runtime_asset_roots)
    roots = (package_root, input_root, output_root, *asset_roots)
    if any(root == Path(root.anchor) for root in roots):
        raise SandboxRuntimeError("sandbox roots are unsafe")
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise SandboxRuntimeError("sandbox roots must not overlap")
    entrypoint = Path(worker_command[0])
    if not entrypoint.is_absolute() or entrypoint.is_symlink():
        raise SandboxRuntimeError("worker entrypoint must be an absolute package file")
    try:
        entrypoint.resolve(strict=True).relative_to(package_root)
    except (FileNotFoundError, ValueError) as exc:
        raise SandboxRuntimeError("worker entrypoint is outside the package") from exc

    policy = {
        "filesystem": {
            "denyRead": [_filesystem_root(package_root)],
            "allowRead": sorted(
                {
                    *(str(path) for path in _system_read_roots()),
                    *(str(path) for path in _seccomp_helper_roots(launcher)),
                    str(package_root),
                    str(input_root),
                    str(output_root),
                    *(str(path) for path in asset_roots),
                }
            ),
            "allowWrite": [str(output_root)],
            "denyWrite": [],
        },
        "network": {
            "allowedDomains": [],
            "deniedDomains": [],
            "allowLocalBinding": False,
            "allowAllUnixSockets": False,
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }
    settings_path = input_root.parent / "sandbox-settings.json"
    _write_private_policy(settings_path, policy)

    return [*launcher, "-s", str(settings_path), *worker_command]


def prepare_agent_shell_command(
    *,
    launcher: tuple[str, ...],
    command: str,
    workspace_root: Path,
    scratch_root: Path,
    executable_roots: tuple[Path, ...] = (),
) -> list[str]:
    """Wrap a host command in a no-network, read-only-workspace SRT policy."""

    if not launcher or not command:
        raise SandboxRuntimeError("sandbox launcher and command are required")
    launcher_path = Path(launcher[0])
    if not launcher_path.is_absolute() or not launcher_path.is_file():
        raise SandboxRuntimeError("sandbox launcher must be an absolute file")
    workspace_root = workspace_root.resolve(strict=True)
    scratch_root = scratch_root.resolve(strict=True)
    if workspace_root == Path(workspace_root.anchor) or scratch_root == Path(scratch_root.anchor):
        raise SandboxRuntimeError("sandbox roots are unsafe")
    if (
        workspace_root == scratch_root
        or workspace_root in scratch_root.parents
        or scratch_root in workspace_root.parents
    ):
        raise SandboxRuntimeError("sandbox roots must not overlap")
    readable_executables = tuple(
        path.resolve(strict=True) for path in executable_roots if path.exists() and path.is_dir()
    )
    policy = {
        "filesystem": {
            "denyRead": [_filesystem_root(workspace_root)],
            "allowRead": sorted(
                {
                    *(str(path) for path in _system_read_roots()),
                    *(str(path) for path in _seccomp_helper_roots(launcher)),
                    str(workspace_root),
                    str(scratch_root),
                    *(str(path) for path in readable_executables),
                }
            ),
            "allowWrite": [str(scratch_root)],
            "denyWrite": [],
        },
        "network": {
            "allowedDomains": [],
            "deniedDomains": [],
            "allowLocalBinding": False,
            "allowAllUnixSockets": False,
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }
    settings_path = scratch_root / "sandbox-settings.json"
    _write_private_policy(settings_path, policy)
    return [*launcher, "-s", str(settings_path), "-c", command]


def _write_private_policy(settings_path: Path, policy: dict[str, object]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(settings_path, flags, 0o600)
    except OSError as exc:
        raise SandboxRuntimeError("cannot create private sandbox policy") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(policy, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


def _filesystem_root(path: Path) -> str:
    if os.name == "nt":
        anchor = path.anchor
        if not anchor:
            raise SandboxRuntimeError("worker package has no filesystem root")
        return anchor
    return "/"


def _system_read_roots() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        candidates = (
            "/System",
            "/usr/lib",
            "/usr/share/locale",
            "/private/var/db/timezone",
            "/dev/null",
            "/dev/urandom",
        )
    elif sys.platform.startswith("linux"):
        candidates = (
            "/lib",
            "/lib64",
            "/usr/lib",
            "/usr/lib64",
            "/usr/share/locale",
            "/etc/ld.so.cache",
            "/etc/localtime",
            "/dev/null",
            "/dev/urandom",
            "/proc/self",
        )
    elif os.name == "nt":
        system_root = os.environ.get("SystemRoot")
        if not system_root:
            raise SandboxRuntimeError("SystemRoot is unavailable")
        candidates = (system_root,)
    else:
        raise SandboxRuntimeError("managed worker sandbox is unsupported on this platform")
    return tuple(Path(candidate).resolve() for candidate in candidates if Path(candidate).exists())
