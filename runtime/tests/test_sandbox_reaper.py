from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.plugins.sandbox_runtime import read_process_start
from shejane_runtime.sandbox_reaper import reap_sandbox_processes
from shejane_runtime.store.sqlite import LocalStore


@contextmanager
def _sandbox_stand_in(marker: str) -> Iterator[subprocess.Popen[bytes]]:
    """A session-leading tree shaped like launcher-over-command."""

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


async def _open_run(tmp_path: Path) -> tuple[LocalStore, str]:
    store = await LocalStore.open(tmp_path / "store.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="sandbox reaping",
        workspace_path=None,
    )
    return store, str(run["id"])


async def _statuses(store: LocalStore) -> list[str]:
    cursor = await store._conn.execute("SELECT status FROM local_sandbox_processes ORDER BY id")
    return [str(row["status"]) for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_reaper_stops_a_sandbox_left_by_a_killed_runtime(tmp_path: Path) -> None:
    store, run_id = await _open_run(tmp_path)
    marker = f"shejane-reap-{uuid.uuid4().hex}"
    try:
        with _sandbox_stand_in(marker) as process:
            started_at = read_process_start(process.pid)
            assert started_at is not None
            children = await asyncio.to_thread(
                subprocess.run,
                ["pgrep", "-P", str(process.pid)],
                check=False,
                capture_output=True,
                text=True,
            )
            descendants = [int(v) for v in children.stdout.split() if v.isdigit()]
            assert descendants, "stand-in never spawned the wrapped command"
            await store.record_sandbox_process(
                run_id=run_id,
                execution_attempt_id="job_a:1",
                pid=process.pid,
                process_started_at=started_at,
                settings_path=marker,
            )

            # include_running is the boot sweep: nothing marked it orphaned,
            # because the Runtime that owned it never got to run any code.
            summary = await reap_sandbox_processes(store, include_running=True)

            assert summary.reaped == 1
            assert summary.settled is True
            assert read_process_start(process.pid) is None
            for descendant in descendants:
                assert read_process_start(descendant) is None, "wrapped command outlived the sweep"
            assert await _statuses(store) == ["reaped"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reaper_leaves_a_recycled_pid_alone(tmp_path: Path) -> None:
    """The guard that stops a stale record from costing an unrelated process."""

    store, run_id = await _open_run(tmp_path)
    marker = f"shejane-reap-{uuid.uuid4().hex}"
    try:
        with _sandbox_stand_in(marker) as process:
            started_at = read_process_start(process.pid)
            assert started_at is not None
            await store.record_sandbox_process(
                run_id=run_id,
                execution_attempt_id="job_a:1",
                pid=process.pid,
                # A pid wearing a different start time is a different process.
                process_started_at=started_at + "9",
                settings_path=marker,
            )

            summary = await reap_sandbox_processes(store, include_running=True)

            assert summary.stale == 1
            assert summary.reaped == 0
            assert summary.settled is False, "a recycled pid must not read as confirmed cleanup"
            assert read_process_start(process.pid) is not None, "bystander was signalled"
            assert await _statuses(store) == ["stale"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reaper_ignores_records_a_live_attempt_still_owns(tmp_path: Path) -> None:
    """Outside boot, a running record belongs to an execution that is still going."""

    store, run_id = await _open_run(tmp_path)
    marker = f"shejane-reap-{uuid.uuid4().hex}"
    try:
        with _sandbox_stand_in(marker) as process:
            started_at = read_process_start(process.pid)
            assert started_at is not None
            await store.record_sandbox_process(
                run_id=run_id,
                execution_attempt_id="job_a:1",
                pid=process.pid,
                process_started_at=started_at,
                settings_path=marker,
            )

            summary = await reap_sandbox_processes(store)

            assert summary.total == 0
            assert read_process_start(process.pid) is not None
            assert await _statuses(store) == ["running"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expiring_a_lease_hands_its_sandboxes_to_the_reaper(tmp_path: Path) -> None:
    """The non-boot path: a Runtime that lost its lease no longer owns its sandboxes."""

    store = await LocalStore.open(tmp_path / "store.db")
    try:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_sandbox_lease",
            client_message_id="msg_sandbox_lease",
            command_payload={
                "type": "run.start",
                "goal": "inspect",
                "workspace_path": None,
                "model": "auto",
            },
            goal="inspect",
            workspace_path=None,
            mode="auto",
        )
        job = await store.claim_run_job(worker_id="worker-one", lease_seconds=0.0)
        assert job is not None
        attempt_id = f"{job['id']}:{job['lease_generation']}"
        await store.record_sandbox_process(
            run_id=str(run["id"]),
            execution_attempt_id=attempt_id,
            pid=999_000,
            process_started_at="1",
            settings_path="/tmp/gone/sandbox-settings.json",
        )
        assert await _statuses(store) == ["running"]

        # A zero-second lease is already expired, so the next claim reconciles it.
        await store.claim_run_job(worker_id="worker-two", lease_seconds=30.0)

        assert await _statuses(store) == ["orphaned"], (
            "lease expiry must release the sandboxes that attempt was running"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reaper_reports_a_finished_sandbox_as_gone(tmp_path: Path) -> None:
    store, run_id = await _open_run(tmp_path)
    marker = f"shejane-reap-{uuid.uuid4().hex}"
    try:
        with _sandbox_stand_in(marker) as process:
            started_at = read_process_start(process.pid)
            assert started_at is not None
            await store.record_sandbox_process(
                run_id=run_id,
                execution_attempt_id="job_a:1",
                pid=process.pid,
                process_started_at=started_at,
                settings_path=marker,
            )
        # Leaving the block stopped the tree, so the record now names nothing.
        summary = await reap_sandbox_processes(store, include_running=True)

        assert summary.gone == 1
        assert summary.settled is True, "an already-stopped sandbox is confirmed cleanup"
        assert await _statuses(store) == ["gone"]
    finally:
        await store.close()
