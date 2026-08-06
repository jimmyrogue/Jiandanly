from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app
from shejane_runtime.store.sqlite import LocalStore
from tests.helpers import run_command


def test_old_persisted_default_budgets_migrate_without_user_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def seed() -> None:
        store = await LocalStore.open(tmp_path / "runtime.db")
        try:
            await store.patch_runtime_settings(
                {"max_model_calls": 20, "research_search_limit": 3},
                initial_settings={"max_model_calls": 20, "research_search_limit": 3},
            )
        finally:
            await store.close()

    asyncio.run(seed())
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        SHEJANE_FAKE_LLM=True,
        data_dir=tmp_path,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/settings", headers={"Authorization": "Bearer tok"})

    assert response.status_code == 200
    assert response.json()["max_model_calls"] == 100
    assert response.json()["research_search_limit"] == 10
    assert response.json()["plan_first"] == "off"


def test_runtime_settings_persist_and_freeze_into_new_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    headers = {"Authorization": "Bearer tok"}

    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok", SHEJANE_FAKE_LLM=True, data_dir=tmp_path
    )
    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/v1/settings",
            headers=headers,
            json={
                "max_model_calls": 42,
                "max_tool_retries": 1,
                "input_guard": "block",
                "plan_first": "auto",
            },
        )
        assert response.status_code == 200
        assert response.json()["max_model_calls"] == 42
        assert response.json()["version"] == 1

        replay = client.put(
            "/v1/settings",
            headers=headers,
            json={
                "max_model_calls": 42,
                "max_tool_retries": 1,
                "input_guard": "block",
                "plan_first": "auto",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["version"] == 1

        created = client.post(
            "/v1/runs",
            headers=headers,
            json=run_command("Use persisted defaults"),
        )
        assert created.status_code == 200
        frozen = json.loads(created.json()["settings_json"])
        assert frozen["max_model_calls"] == 42
        assert frozen["input_guard"] == "block"

    restarted = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok", SHEJANE_FAKE_LLM=True, data_dir=tmp_path
    )
    with TestClient(create_app(restarted)) as client:
        response = client.get("/v1/settings", headers=headers)
        assert response.status_code == 200
        assert response.json()["max_model_calls"] == 42
        assert response.json()["plan_first"] == "auto"
        assert response.json()["version"] == 1


@pytest.mark.asyncio
async def test_runtime_settings_store_atomically_merges_concurrent_patches(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.db"
    first = await LocalStore.open(database)
    second = await LocalStore.open(database)
    defaults = {"max_model_calls": 20, "max_tool_retries": 2}
    try:
        await asyncio.gather(
            first.patch_runtime_settings({"max_model_calls": 41}, initial_settings=defaults),
            second.patch_runtime_settings({"max_tool_retries": 4}, initial_settings=defaults),
        )
        stored = await first.get_runtime_settings()
        assert stored is not None
        assert stored["settings"]["max_model_calls"] == 41
        assert stored["settings"]["max_tool_retries"] == 4
        assert stored["version"] == 2
    finally:
        await first.close()
        await second.close()
