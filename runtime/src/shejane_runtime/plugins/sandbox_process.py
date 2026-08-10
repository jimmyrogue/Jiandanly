"""Recorded sandbox process identity and safe process-tree reaping."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxProcessIdentity:
    """What must still hold before a recorded sandbox pid may be signalled."""

    pid: int
    started_at: str
    settings_path: str


def read_process_start(pid: int) -> str | None:
    """Return an opaque, stable start-time token for a live process."""

    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        _, _, rest = stat.rpartition(")")
        fields = rest.split()
        if len(fields) <= 19 or fields[0] == "Z":
            return None
        return fields[19]
    completed = subprocess.run(
        ["ps", "-o", "state=,lstart=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    line = completed.stdout.strip()
    if not line:
        return None
    state, _, started = line.partition(" ")
    if state.startswith("Z"):
        return None
    return started.strip() or None


def read_process_command(pid: int) -> str | None:
    """Return a process's full argv as one string, or None when it is gone."""

    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        command = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        return command or None
    completed = subprocess.run(
        ["ps", "-ww", "-o", "args=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    command = completed.stdout.strip()
    return command or None


def sandbox_process_matches(identity: SandboxProcessIdentity) -> bool:
    """Return whether the live process is still the recorded sandbox."""

    if identity.pid <= 1:
        return False
    started_at = read_process_start(identity.pid)
    if started_at is None or started_at != identity.started_at:
        return False
    command = read_process_command(identity.pid)
    return command is not None and identity.settings_path in command


def terminate_sandbox_process(identity: SandboxProcessIdentity) -> str:
    """Safely stop the recorded sandbox process group."""

    if not sandbox_process_matches(identity):
        return "gone" if read_process_start(identity.pid) is None else "stale"
    try:
        group = os.getpgid(identity.pid)
    except ProcessLookupError:
        return "gone"
    except OSError:
        group = None
    target = -group if group is not None and group > 1 else identity.pid
    signalled = False
    for attempt in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(target, attempt)
        except ProcessLookupError:
            return "reaped" if signalled else "gone"
        except PermissionError:
            return "reaped" if signalled else "stale"
        signalled = True
        if _await_process_exit(identity.pid):
            return "reaped"
    return "reaped"


def _await_process_exit(pid: int, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_process_start(pid) is None:
            return True
        time.sleep(0.05)
    return read_process_start(pid) is None
