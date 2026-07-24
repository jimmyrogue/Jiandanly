from __future__ import annotations

import json
import sqlite3

import pytest

from shejane_runtime.eval.seed_service import seed_service
from shejane_runtime.store.sqlite import SCHEMA


def test_seed_service_copies_only_the_selected_connection(tmp_path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    with sqlite3.connect(source / "runtime.db") as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO model_connections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "local:owner",
                "connection",
                "custom",
                "Service",
                "custom",
                "openai_chat",
                "https://example.test/v1",
                1,
                "keyring:model-service:connection",
                json.dumps(
                    [
                        {
                            "model_id": "model",
                            "display_name": "Model",
                            "source": "manual",
                            "verification": "verified",
                            "recommended": False,
                            "tool_calling": True,
                            "streaming": True,
                            "image_inputs": False,
                        }
                    ]
                ),
                "ready",
                3,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO local_runs "
            "(id, graph_thread_id, goal, status, created_at, updated_at) "
            "VALUES ('private-run', 'thread', 'private', 'completed', 'now', 'now')"
        )

    seed_service(source, destination, "local:connection:model")

    with sqlite3.connect(destination / "runtime.db") as connection:
        assert connection.execute("SELECT id FROM model_connections").fetchall() == [
            ("connection",)
        ]
        assert connection.execute("SELECT id FROM local_runs").fetchall() == []


def test_seed_service_rejects_an_unconfigured_model(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with sqlite3.connect(source / "runtime.db") as connection:
        connection.executescript(SCHEMA)

    with pytest.raises(ValueError, match="model service not found"):
        seed_service(source, tmp_path / "destination", "local:missing:model")
