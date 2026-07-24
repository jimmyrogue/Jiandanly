from __future__ import annotations

from types import SimpleNamespace

import pytest

from shejane_runtime import processes


async def _return(value: int) -> int:
    return value


@pytest.mark.asyncio
async def test_windows_process_cleanup_terminates_the_entire_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_kills: list[bool] = []
    process = SimpleNamespace(
        pid=42,
        returncode=None,
        kill=lambda: direct_kills.append(True),
        wait=lambda: _return(0),
    )
    taskkill = SimpleNamespace(wait=lambda: _return(0))
    command: list[object] = []

    async def create_subprocess_exec(*args: object, **_kwargs: object) -> object:
        command.extend(args)
        return taskkill

    monkeypatch.setattr(processes.os, "name", "nt")
    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", create_subprocess_exec)

    await processes.kill_process_tree(process)

    assert command[:6] == ["taskkill", "/PID", "42", "/T", "/F"]
    assert direct_kills == []
