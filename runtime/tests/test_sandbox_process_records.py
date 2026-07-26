from __future__ import annotations

from pathlib import Path

import pytest

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.store.sqlite import LocalStore


async def _open_run(tmp_path: Path) -> tuple[LocalStore, str]:
    store = await LocalStore.open(tmp_path / "store.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="sandbox process bookkeeping",
        workspace_path=None,
    )
    return store, str(run["id"])


async def _sandbox_rows(store: LocalStore) -> list[dict[str, object]]:
    cursor = await store._conn.execute(
        "SELECT * FROM local_sandbox_processes ORDER BY created_at, id"
    )
    return [dict(row) for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_recording_a_sandbox_process_survives_for_a_later_runtime(tmp_path: Path) -> None:
    store, run_id = await _open_run(tmp_path)
    try:
        record_id = await store.record_sandbox_process(
            run_id=run_id,
            execution_attempt_id="job_a:1",
            pid=4242,
            process_started_at="884422",
            settings_path="/tmp/shejane-agent-shell-abc/sandbox-settings.json",
        )

        rows = await _sandbox_rows(store)
        assert len(rows) == 1
        assert rows[0]["id"] == record_id
        assert rows[0]["pid"] == 4242
        assert rows[0]["status"] == "running"
        # The two fields that later prove the pid was not recycled.
        assert rows[0]["process_started_at"] == "884422"
        assert rows[0]["settings_path"].endswith("sandbox-settings.json")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_forgetting_a_supervised_launcher_leaves_nothing_to_reap(tmp_path: Path) -> None:
    store, run_id = await _open_run(tmp_path)
    try:
        record_id = await store.record_sandbox_process(
            run_id=run_id,
            execution_attempt_id="job_a:1",
            pid=4242,
            process_started_at="884422",
            settings_path="/tmp/shejane-agent-shell-abc/sandbox-settings.json",
        )

        await store.forget_sandbox_process(record_id)

        assert await _sandbox_rows(store) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sandbox_records_are_scoped_to_one_execution_attempt(tmp_path: Path) -> None:
    """A retry must not inherit the previous attempt's processes."""

    store, run_id = await _open_run(tmp_path)
    try:
        await store.record_sandbox_process(
            run_id=run_id,
            execution_attempt_id="job_a:1",
            pid=1001,
            process_started_at="111",
            settings_path="/tmp/one/sandbox-settings.json",
        )
        await store.record_sandbox_process(
            run_id=run_id,
            execution_attempt_id="job_a:2",
            pid=1002,
            process_started_at="222",
            settings_path="/tmp/two/sandbox-settings.json",
        )

        rows = await _sandbox_rows(store)
        attempts = {str(row["execution_attempt_id"]): int(row["pid"]) for row in rows}
        assert attempts == {"job_a:1": 1001, "job_a:2": 1002}
    finally:
        await store.close()
