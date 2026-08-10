"""Fail-closed Managed Worker release evidence by target platform."""

from __future__ import annotations

from dataclasses import dataclass


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
            blockers=("packaged_backend_absent", "release_ci_gate"),
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
