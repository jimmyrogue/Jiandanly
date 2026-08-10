"""One-shot Managed Worker protocol spike.

This proves bounded invocation and staging semantics.  It deliberately does
not claim that a child process is an OS permission sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .linux_cgroup import LinuxCgroupResources, prepare_linux_cgroup_command
from .managed_worker_protocol import (
    WorkerProtocolError as WorkerProtocolError,
)
from .managed_worker_protocol import (
    _bounded_stderr,
    _progress_notification,
    _read_frame,
    _request_cooperative_cancel,
    _validate_result_identity,
    _validate_staged_artifacts,
    _write_frame,
)
from .managed_worker_protocol import (
    _terminate_process_tree as _terminate_process_tree,
)
from .managed_worker_protocol import (
    _vision_request as _vision_request,
)
from .sandbox_contract import SandboxLimits
from .sandbox_runtime import prepare_srt_command

if TYPE_CHECKING:
    from .macos_vm import MacOSVMResources
    from .runtime_assets import RuntimeAssetHandle


_VM_STAGING_PREFIX = "vm-invocation-"
_VM_STAGING_LEASE = ".lease.lock"


def _create_vm_staging(parent: Path) -> tuple[Path, int]:
    """Create crash-recoverable VM staging and hold its inherited lease."""

    import fcntl

    parent = parent.resolve(strict=True)
    _reap_stale_vm_staging(parent)
    staging = Path(tempfile.mkdtemp(prefix=_VM_STAGING_PREFIX, dir=parent))
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    lease_fd: int | None = None
    try:
        lease_fd = os.open(staging / _VM_STAGING_LEASE, flags, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        if lease_fd is not None:
            os.close(lease_fd)
        shutil.rmtree(staging)
        raise
    return staging, lease_fd


def _reap_stale_vm_staging(parent: Path) -> None:
    """Remove only well-formed staging whose Runtime/launcher lease is free."""

    import fcntl

    parent = parent.resolve(strict=True)
    for candidate in parent.glob(f"{_VM_STAGING_PREFIX}*"):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            continue
        lease = candidate / _VM_STAGING_LEASE
        try:
            lease_metadata = lease.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(lease_metadata.st_mode)
            or lease_metadata.st_uid != os.getuid()
            or lease_metadata.st_nlink != 1
            or lease_metadata.st_mode & 0o077
        ):
            continue
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            lease_fd = os.open(lease, flags)
        except OSError:
            continue
        try:
            opened = os.fstat(lease_fd)
            if (opened.st_dev, opened.st_ino) != (
                lease_metadata.st_dev,
                lease_metadata.st_ino,
            ):
                continue
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            shutil.rmtree(candidate)
        finally:
            os.close(lease_fd)


def _release_vm_staging(staging: Path, lease_fd: int) -> None:
    try:
        shutil.rmtree(staging)
    finally:
        os.close(lease_fd)


async def invoke_managed_worker(
    *,
    command: list[str],
    invocation: dict[str, Any],
    input_root: Path,
    output_root: Path,
    sandbox_command: tuple[str, ...] | None = None,
    linux_cgroup: LinuxCgroupResources | None = None,
    vm_resources: MacOSVMResources | None = None,
    package_root: Path | None = None,
    runtime_assets: tuple[RuntimeAssetHandle, ...] = (),
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    vision_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    max_frame_bytes: int = 1024 * 1024,
    max_stderr_bytes: int = 64 * 1024,
    max_progress_frames: int = 100_000,
    max_progress_phases: int = 64,
    progress_interval_seconds: float = 0.25,
    cancel_grace_seconds: float = 0.25,
) -> dict[str, Any]:
    """Run exactly one Action in a fresh process, optionally behind SRT."""

    if not command:
        raise ValueError("managed worker command is empty")
    sandbox_limits = SandboxLimits.from_action_limits(invocation["limits"])
    max_frame_bytes = min(max_frame_bytes, sandbox_limits.protocol_frame_bytes)
    max_stderr_bytes = min(max_stderr_bytes, sandbox_limits.stderr_bytes)
    input_root = input_root.resolve(strict=True)
    output_root = output_root.resolve(strict=True)
    if vm_resources is not None and (sandbox_command is not None or linux_cgroup is not None):
        raise ValueError("managed worker cannot combine VM and native sandbox backends")
    if (
        sandbox_command is not None
        and linux_cgroup is not None
        and linux_cgroup.bubblewrap is not None
    ):
        raise ValueError("managed worker cannot combine SRT and native bubblewrap")
    vm_staging: Path | None = None
    vm_lease_fd: int | None = None
    linux_sandboxed = linux_cgroup is not None and linux_cgroup.bubblewrap is not None
    access_isolated = sandbox_command is not None or vm_resources is not None or linux_sandboxed
    # A cgroup-only Linux wrapper remains a partial resource layer. The native
    # bubblewrap path adds the fixed-capacity private scratch required by the contract.
    resource_isolated = vm_resources is not None or linux_sandboxed
    sandboxed = access_isolated and resource_isolated
    if vm_resources is not None:
        from .macos_vm import prepare_macos_vm_command

        if package_root is None:
            raise ValueError("VM managed worker requires package_root")
        vm_staging, vm_lease_fd = await asyncio.to_thread(
            _create_vm_staging,
            input_root.parent,
        )
        try:
            command = await asyncio.to_thread(
                prepare_macos_vm_command,
                resources=vm_resources,
                limits=sandbox_limits,
                package_root=package_root,
                entrypoint=Path(command[0]),
                input_root=input_root,
                output_root=output_root,
                runtime_assets=runtime_assets,
                temporary_root=vm_staging,
            )
        except BaseException:
            await asyncio.to_thread(_release_vm_staging, vm_staging, vm_lease_fd)
            raise
    elif sandbox_command is not None:
        if package_root is None:
            raise ValueError("sandboxed managed worker requires package_root")
        command = prepare_srt_command(
            launcher=sandbox_command,
            worker_command=command,
            package_root=package_root,
            input_root=input_root,
            output_root=output_root,
            runtime_asset_roots=tuple(asset.payload for asset in runtime_assets),
        )
    if linux_cgroup is not None:
        command = prepare_linux_cgroup_command(
            resources=linux_cgroup,
            limits=sandbox_limits,
            command=command,
            package_root=package_root,
            input_root=input_root,
            output_root=output_root,
            runtime_asset_roots={asset.asset_id: asset.payload for asset in runtime_assets},
        )
    ordered_assets = tuple(sorted(runtime_assets, key=lambda asset: asset.asset_id))
    if len({asset.asset_id for asset in ordered_assets}) != len(ordered_assets):
        raise ValueError("managed worker runtime asset ids must be unique")
    timeout_seconds = sandbox_limits.wall_time_ms / 1000
    try:
        process_options: dict[str, Any] = {}
        if vm_lease_fd is not None:
            process_options["pass_fds"] = (vm_lease_fd,)
        process_environment = {
            "PATH": os.defpath,
            "PYTHONUTF8": "1",
            "ELECTRON_RUN_AS_NODE": "1",
            "SHEJANE_PLUGIN_INPUT_ROOT": str(input_root),
            "SHEJANE_PLUGIN_OUTPUT_ROOT": str(output_root),
            "SHEJANE_PLUGIN_ACCESS_ISOLATED": "1" if access_isolated else "0",
            "SHEJANE_PLUGIN_RESOURCE_ISOLATED": "1" if resource_isolated else "0",
            "SHEJANE_PLUGIN_SANDBOXED": "1" if sandboxed else "0",
            "SHEJANE_PLUGIN_RUNTIME_ASSETS": json.dumps(
                {asset.asset_id: str(asset.payload) for asset in ordered_assets},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        if os.name == "nt":
            for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
                if value := os.environ.get(name):
                    process_environment[name] = value
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_environment,
            cwd=(str(package_root) if package_root is not None and vm_resources is None else None),
            start_new_session=os.name != "nt",
            limit=max_frame_bytes + 1,
            **process_options,
        )
    except BaseException:
        if vm_staging is not None and vm_lease_fd is not None:
            await asyncio.to_thread(_release_vm_staging, vm_staging, vm_lease_fd)
        raise
    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr
    assert stdin and stdout and stderr
    stderr_task = asyncio.create_task(_bounded_stderr(stderr, max_stderr_bytes))

    progress_sequence = 0
    progress_count = 0
    progress_phases: set[str] = set()
    latest_progress: dict[str, Any] | None = None
    last_emitted_sequence = 0
    last_emitted_at = 0.0
    invoke_started = False
    vision_call_count = 0

    def emit_progress(progress: dict[str, Any], *, force: bool = False) -> None:
        nonlocal last_emitted_at, last_emitted_sequence, latest_progress
        latest_progress = progress
        if on_progress is None or progress["sequence"] == last_emitted_sequence:
            return
        now = time.monotonic()
        phase = str(progress["phase"])
        phase_changed = phase not in progress_phases and len(progress_phases) < max_progress_phases
        if not force and not phase_changed and now - last_emitted_at < progress_interval_seconds:
            return
        progress_phases.add(phase)
        on_progress(dict(progress))
        last_emitted_at = now
        last_emitted_sequence = int(progress["sequence"])

    async def exchange(
        request_id: int,
        method: str,
        params: dict[str, Any],
        *,
        allow_progress: bool = False,
    ) -> Any:
        nonlocal progress_count, progress_sequence, vision_call_count
        await _write_frame(
            stdin,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            max_frame_bytes,
        )
        while True:
            response = await _read_frame(stdout, max_frame_bytes)
            if "method" in response:
                if "id" in response:
                    if not allow_progress or vision_handler is None:
                        raise WorkerProtocolError(f"worker emitted a host request during {method}")
                    params = _vision_request(response, invocation)
                    vision_call_count += 1
                    if vision_call_count > 1:
                        raise WorkerProtocolError("managed worker vision host-call limit exceeded")
                    host_result = await vision_handler(params)
                    if not isinstance(host_result, dict):
                        raise WorkerProtocolError("vision host call returned an invalid result")
                    await _write_frame(
                        stdin,
                        {
                            "jsonrpc": "2.0",
                            "id": response["id"],
                            "result": host_result,
                        },
                        max_frame_bytes,
                    )
                    continue
                if not allow_progress:
                    raise WorkerProtocolError(f"worker emitted a notification during {method}")
                progress_sequence = _progress_notification(
                    response,
                    invocation=invocation,
                    previous_sequence=progress_sequence,
                )
                progress_count += 1
                if progress_count > max_progress_frames:
                    raise WorkerProtocolError("managed worker progress limit exceeded")
                emit_progress(dict(response["params"]))
                continue
            if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
                raise WorkerProtocolError(f"invalid {method} response identity")
            if "error" in response:
                raise WorkerProtocolError(f"worker rejected {method}: {response['error']}")
            if "result" not in response:
                raise WorkerProtocolError(f"worker omitted {method} result")
            if allow_progress and latest_progress is not None:
                emit_progress(latest_progress, force=True)
            return response["result"]

    try:
        async with asyncio.timeout(timeout_seconds):
            if vm_resources is not None:
                ready = await _read_frame(stdout, max_frame_bytes)
                if ready != {
                    "cpu_max": "100000 100000",
                    "input_read_only": True,
                    "memory_bytes": sandbox_limits.memory_bytes,
                    "output_bytes": sandbox_limits.output_bytes,
                    "package_read_only": True,
                    "pids_max": sandbox_limits.process_count,
                    "protocol_version": 1,
                    "rootfs_read_only": True,
                    "scratch_bytes": sandbox_limits.scratch_bytes,
                    "type": "ready",
                }:
                    raise WorkerProtocolError("managed worker VM attestation changed")
            action = invocation["action"]
            initialized = await exchange(
                1,
                "initialize",
                {
                    "protocol_version": 1,
                    "plugin_id": action["plugin_id"],
                    "plugin_digest": action["plugin_digest"],
                    "actions": [action["action_id"]],
                    "granted_capabilities": invocation["grants"]["capabilities"],
                    "limits": invocation["limits"],
                    "runtime_assets": [
                        {
                            "id": asset.asset_id,
                            "version": asset.version,
                            "digest": asset.digest,
                        }
                        for asset in ordered_assets
                    ],
                },
            )
            if initialized != {
                "protocol_version": 1,
                "process_isolated": True,
                "access_isolated": access_isolated,
                "resource_isolated": resource_isolated,
                "sandboxed": sandboxed,
            }:
                raise WorkerProtocolError("worker returned an unsupported isolation state")

            invoke_started = True
            result = await exchange(2, "invoke", invocation, allow_progress=True)
            _validate_result_identity(result, invocation)
            _validate_staged_artifacts(result, output_root, invocation["limits"]["output_mb"])
            await exchange(3, "shutdown", {})
            stdin.close()
            await process.wait()
            stderr_bytes = await stderr_task
            if process.returncode != 0:
                raise WorkerProtocolError(
                    "worker exited with status "
                    f"{process.returncode}: {stderr_bytes.decode(errors='replace')}"
                )
            return result
    except BaseException as exc:
        stopped = False
        if invoke_started:
            reason = (
                "timeout"
                if isinstance(exc, TimeoutError)
                else "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "host_error"
            )
            stopped = await _request_cooperative_cancel(
                process,
                stdin,
                invocation,
                reason=reason,
                max_frame_bytes=max_frame_bytes,
                grace_seconds=(
                    max(1.0, cancel_grace_seconds)
                    if vm_resources is not None
                    else cancel_grace_seconds
                ),
            )
        if not stopped:
            await _terminate_process_tree(
                process,
                allow_supervisor_cleanup=linux_cgroup is not None,
            )
        stderr_bytes = await stderr_task
        if isinstance(exc, WorkerProtocolError) and stderr_bytes:
            detail = stderr_bytes.decode(errors="replace").strip()
            raise WorkerProtocolError(f"{exc}: {detail}") from exc
        raise
    finally:
        if vm_staging is not None and vm_lease_fd is not None:
            await asyncio.to_thread(_release_vm_staging, vm_staging, vm_lease_fd)
