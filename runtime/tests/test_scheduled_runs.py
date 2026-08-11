"""Scheduled local runs.

These tests lock the minimal P2 local scheduler contract:

- schedules are persisted in SQLite and due rows are claimed once;
- the dispatcher creates a normal local run through RunCoordinator and drains
  its stream so background runs cannot fill the live queue;
- HTTP routes expose create/list/cancel/notified for the Electron renderer.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import Settings, reset_settings_for_tests
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.scheduler import ScheduledRunDispatcher
from shejane_runtime.server import create_app
from shejane_runtime.store.sqlite import LocalStore

HEADERS = {"Authorization": "Bearer tok"}


class FakeCoordinator:
    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self.started: list[dict[str, Any]] = []

    async def start_run(self, **kwargs: Any) -> dict[str, Any]:
        self.started.append(kwargs)
        run = await self.store.create_run(
            principal_id=kwargs["principal_id"],
            goal=kwargs["goal"],
            workspace_path=kwargs.get("workspace_path"),
            parent_run_id=None,
            settings=kwargs.get("settings"),
            metadata=kwargs.get("metadata"),
            mode=kwargs.get("mode", "auto"),
        )
        await self.store.append_event(run["id"], "run.completed", {"final_text": "scheduled done"})
        await self.store.update_run_status(run["id"], "completed")
        return run

    async def stream(self, _run_id: str):
        if False:
            yield {}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-schedules-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


async def test_store_claims_due_schedules_once(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        now = datetime.now(UTC)
        await store.create_workspace(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            path=str(tmp_path),
            label="test",
        )
        due = await store.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="整理日报",
            run_at=(now - timedelta(seconds=5)).isoformat(),
            workspace_path=str(tmp_path),
            model="auto",
            history=[{"role": "user", "content": "昨天做了什么"}],
            settings={"memory": "on"},
            metadata={"source": "test"},
        )
        future = await store.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="明天再跑",
            run_at=(now + timedelta(hours=1)).isoformat(),
        )

        claimed = await store.claim_due_scheduled_runs(now=now.isoformat())
        assert [row["id"] for row in claimed] == [due["id"]]
        assert claimed[0]["status"] == "running"
        assert json.loads(claimed[0]["history_json"]) == [
            {"role": "user", "content": "昨天做了什么"}
        ]

        assert await store.claim_due_scheduled_runs(now=now.isoformat()) == []
        assert (await store.get_scheduled_run(future["id"]))["status"] == "scheduled"
    finally:
        await store.close()


async def test_concurrent_stores_cannot_claim_the_same_schedule(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    first = await LocalStore.open(db_path)
    second = await LocalStore.open(db_path)
    try:
        now = datetime.now(UTC)
        schedule = await first.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="run once",
            run_at=(now - timedelta(seconds=1)).isoformat(),
        )

        claims = await asyncio.gather(
            first.claim_due_scheduled_runs(now=now.isoformat()),
            second.claim_due_scheduled_runs(now=now.isoformat()),
        )

        assert [row["id"] for batch in claims for row in batch] == [schedule["id"]]
    finally:
        await first.close()
        await second.close()


async def test_recover_running_retries_a_claim_interrupted_before_run_creation(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        now = datetime.now(UTC)
        schedule = await store.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="resume after restart",
            run_at=(now - timedelta(seconds=1)).isoformat(),
        )
        assert await store.claim_due_scheduled_runs(now=now.isoformat())

        coordinator = FakeCoordinator(store)
        dispatcher = ScheduledRunDispatcher(store=store, coordinator=coordinator)
        await dispatcher.recover_running()

        recovered = await store.get_scheduled_run(schedule["id"])
        assert recovered is not None
        assert recovered["status"] == "completed"
        assert recovered["run_id"] is not None
        assert len(coordinator.started) == 1
    finally:
        await store.close()


async def test_scheduled_run_terminal_state_and_run_binding_are_immutable(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        now = datetime.now(UTC)
        schedule = await store.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="run once",
            run_at=(now - timedelta(seconds=1)).isoformat(),
        )
        assert await store.claim_due_scheduled_runs(now=now.isoformat())
        first_run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="first",
            workspace_path=None,
        )
        second_run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="second",
            workspace_path=None,
        )

        await store.mark_scheduled_run_started(schedule["id"], first_run["id"])
        completed = await store.complete_scheduled_run(
            schedule["id"],
            status="completed",
            result_text="first result",
        )
        rebound = await store.mark_scheduled_run_started(schedule["id"], second_run["id"])
        overwritten = await store.complete_scheduled_run(
            schedule["id"],
            status="failed",
            error_message="late failure",
        )

        assert completed is not None
        assert rebound == completed
        assert overwritten == completed
    finally:
        await store.close()


async def test_schedule_notification_is_terminal_only_and_idempotent(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        now = datetime.now(UTC)
        schedule = await store.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="notify once",
            run_at=(now - timedelta(seconds=1)).isoformat(),
        )
        premature = await store.mark_scheduled_run_notified(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            schedule_id=schedule["id"],
        )
        assert premature is not None
        assert premature["notified_at"] is None

        assert await store.claim_due_scheduled_runs(now=now.isoformat())
        await store.complete_scheduled_run(schedule["id"], status="completed")
        first = await store.mark_scheduled_run_notified(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            schedule_id=schedule["id"],
        )
        second = await store.mark_scheduled_run_notified(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            schedule_id=schedule["id"],
        )

        assert first is not None
        assert first["notified_at"] is not None
        assert second == first
    finally:
        await store.close()


async def test_legacy_schedules_migrate_to_the_local_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE local_scheduled_runs (
            id TEXT PRIMARY KEY, goal TEXT NOT NULL, workspace_path TEXT,
            model TEXT NOT NULL DEFAULT 'auto', history_json TEXT NOT NULL DEFAULT '[]',
            settings_json TEXT NOT NULL DEFAULT '{}', metadata_json TEXT NOT NULL DEFAULT '{}',
            run_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', run_id TEXT,
            result_text TEXT, error_message TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, completed_at TEXT, notified_at TEXT
        );
        INSERT INTO local_scheduled_runs
            (id, goal, run_at, created_at, updated_at)
            VALUES ('sched_legacy', 'legacy', '2026-01-02', '2026-01-01', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    store = await LocalStore.open(db_path)
    try:
        schedule = await store.get_scheduled_run("sched_legacy")
        assert schedule is not None
        assert schedule["principal_id"] == LOCAL_OWNER_PRINCIPAL_ID
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("schedule_settings", "expected_reasoning_mode", "settings_are_frozen"),
    [
        ({"skills": "on", "reasoning_mode": "xhigh"}, "xhigh", False),
        ({"_snapshot_version": 1, "skills": "on"}, "off", True),
        ({}, "off", False),
    ],
)
async def test_dispatcher_starts_due_schedule_and_marks_completed(
    tmp_path: Path,
    schedule_settings: dict[str, Any],
    expected_reasoning_mode: str,
    settings_are_frozen: bool,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        now = datetime.now(UTC)
        await store.create_workspace(
            principal_id="user:scheduled-owner",
            path=str(tmp_path),
            label="test",
        )
        schedule = await store.create_scheduled_run(
            principal_id="user:scheduled-owner",
            goal="跑一次本地检查",
            run_at=(now - timedelta(seconds=1)).isoformat(),
            workspace_path=str(tmp_path),
            model="auto",
            settings=schedule_settings,
            metadata={"kind": "nightly"},
        )
        coordinator = FakeCoordinator(store)
        dispatcher = ScheduledRunDispatcher(store=store, coordinator=coordinator, poll_interval=60)

        await dispatcher.tick(now=now)

        updated = await store.get_scheduled_run(schedule["id"])
        assert updated["status"] == "completed"
        assert updated["run_id"].startswith("run_")
        assert updated["result_text"] == "scheduled done"
        assert coordinator.started == [
            {
                "principal_id": "user:scheduled-owner",
                "command_id": f"cmd_schedule:{schedule['id']}",
                "client_message_id": f"msg_schedule:{schedule['id']}",
                "protocol_version": 1,
                "required_capabilities": ["agent.run", "agent.stream"],
                "goal": "跑一次本地检查",
                "workspace_path": str(tmp_path),
                "mode": "auto",
                "reasoning_mode": expected_reasoning_mode,
                "history": [],
                "settings": schedule_settings,
                "settings_are_frozen": settings_are_frozen,
                "metadata_is_trusted": True,
                "metadata": {
                    "intent": "scheduled_run",
                    "scheduled_run_id": schedule["id"],
                },
            }
        ]
    finally:
        await store.close()


async def test_dispatcher_rejects_unknown_settings_snapshot_version(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        now = datetime.now(UTC)
        schedule = await store.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="inspect",
            run_at=(now - timedelta(seconds=1)).isoformat(),
            settings={"_snapshot_version": 2},
        )
        coordinator = RunCoordinator(
            store=store,
            checkpointer=None,  # type: ignore[arg-type]
            settings=Settings(SHEJANE_FAKE_LLM=True),
        )
        dispatcher = ScheduledRunDispatcher(store=store, coordinator=coordinator)

        await dispatcher.tick(now=now)

        failed = await store.get_scheduled_run(schedule["id"])
        assert failed is not None and failed["status"] == "failed"
        assert "snapshot version is unsupported" in failed["error_message"]
    finally:
        await store.close()


def test_schedule_http_create_list_cancel_and_mark_notified(client: TestClient) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    workspace = Path(tempfile.mkdtemp(prefix="jdl-schedule-workspace-"))
    authorized = client.post(
        "/v1/workspaces",
        headers=HEADERS,
        json={"path": str(workspace), "label": "schedule"},
    )
    assert authorized.status_code == 200
    created = client.post(
        "/v1/schedules",
        headers=HEADERS,
        json={
            "goal": "稍后总结项目",
            "run_at": future,
            "workspace_path": str(workspace),
            "model": "local:test:model",
            "reasoning_mode": "xhigh",
            "permission_mode": "auto",
            "history": [{"role": "user", "content": "项目背景"}],
            "settings": {"memory": "on", "api_key": "must-not-persist"},
            "metadata": {"token": "must-not-persist"},
        },
    )
    assert created.status_code == 200
    schedule = created.json()
    assert schedule["id"].startswith("sched_")
    assert schedule["status"] == "scheduled"
    assert schedule["goal"] == "稍后总结项目"
    schedule_settings = json.loads(schedule["settings_json"])
    assert schedule_settings["_snapshot_version"] == 1
    assert schedule_settings["memory"] == "on"
    assert schedule_settings["permission_mode"] == "auto"
    assert schedule_settings["reasoning_mode"] == "xhigh"
    assert "must-not-persist" not in schedule["settings_json"]
    assert json.loads(schedule["metadata_json"]) == {}

    listed = client.get("/v1/schedules", headers=HEADERS)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["schedules"]] == [schedule["id"]]

    store = client.app.state.store

    async def finish_schedule() -> None:
        assert await store.claim_due_scheduled_runs(now=future)
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="done",
            workspace_path=None,
        )
        await store.mark_scheduled_run_started(schedule["id"], run["id"])
        await store.complete_scheduled_run(
            schedule["id"],
            status="completed",
            result_text="计划任务完成",
        )

    asyncio.run(finish_schedule())
    pending = client.get("/v1/schedules?notify_pending=true", headers=HEADERS)
    assert pending.status_code == 200
    assert pending.json()["schedules"][0]["result_text"] == "计划任务完成"

    marked = client.post(f"/v1/schedules/{schedule['id']}/notified", headers=HEADERS)
    assert marked.status_code == 200
    assert marked.json()["notified_at"]
    assert client.get("/v1/schedules?notify_pending=true", headers=HEADERS).json() == {
        "schedules": []
    }

    other_response = client.post(
        "/v1/schedules",
        headers=HEADERS,
        json={"goal": "取消这个", "run_at": future, "model": "local:test:model"},
    )
    other = other_response.json()
    assert json.loads(other["settings_json"])["reasoning_mode"] == "off"
    canceled = client.delete(f"/v1/schedules/{other['id']}", headers=HEADERS)
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"


def test_foreign_schedule_is_hidden_from_local_owner(client: TestClient) -> None:
    store = client.app.state.store

    async def create_foreign_schedule() -> str:
        schedule = await store.create_scheduled_run(
            principal_id="user:foreign",
            goal="private",
            run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        return schedule["id"]

    schedule_id = asyncio.run(create_foreign_schedule())

    listed = client.get("/v1/schedules", headers=HEADERS)
    assert listed.json() == {"schedules": []}
    canceled = client.delete(f"/v1/schedules/{schedule_id}", headers=HEADERS)
    assert canceled.status_code == 404
    notified = client.post(f"/v1/schedules/{schedule_id}/notified", headers=HEADERS)
    assert notified.status_code == 404


async def test_dispatcher_rejects_a_revoked_workspace(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        workspace = await store.create_workspace(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            path=str(tmp_path),
            label="test",
        )
        schedule = await store.create_scheduled_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="private",
            workspace_path=str(tmp_path),
            run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        assert await store.delete_workspace(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            workspace_id=workspace["id"],
        )
        coordinator = FakeCoordinator(store)
        dispatcher = ScheduledRunDispatcher(store=store, coordinator=coordinator)

        await dispatcher.tick()

        updated = await store.get_scheduled_run(schedule["id"])
        assert updated is not None
        assert updated["status"] == "failed"
        assert updated["run_id"] is None
        assert coordinator.started == []
    finally:
        await store.close()
