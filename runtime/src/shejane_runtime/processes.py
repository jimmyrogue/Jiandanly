from __future__ import annotations

import asyncio
import contextlib
import os
import signal


async def kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Hard-stop a subprocess tree and reap its direct process."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        taskkill_status = -1
        try:
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            taskkill_status = await taskkill.wait()
        except (FileNotFoundError, ProcessLookupError):
            pass
        if taskkill_status != 0 and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        await process.wait()
