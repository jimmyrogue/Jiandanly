from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from shejane_runtime.a2a_gateway.store import (
    A2AGatewayStore,
    A2AMessageConflictError,
)


async def _peer(store: A2AGatewayStore, tenant: str = "research") -> dict[str, object]:
    peer, _token = await store.create_peer(
        name=tenant,
        tenant=tenant,
        scopes=["tasks.create", "tasks.read"],
        runtime_model="local:test:model",
        runtime_workspace_path=None,
        permission_mode="ask",
        push_origins=[],
        expires_at=None,
    )
    return peer


def _ids(seed: str) -> dict[str, str]:
    suffix = hashlib.sha256(seed.encode()).hexdigest()[:24]
    return {
        "new_task_id": f"task_{suffix}",
        "new_context_id": f"ctx_{suffix}",
        "runtime_thread_id": f"a2a_thread_{suffix}",
        "runtime_command_id": f"a2a_command_{suffix}",
        "runtime_client_message_id": f"a2a_message_{suffix}",
    }


@pytest.mark.asyncio
async def test_message_preparation_is_atomic_idempotent_and_tenant_scoped(
    tmp_path: Path,
) -> None:
    store = await A2AGatewayStore.open(tmp_path / "gateway.db")
    try:
        peer = await _peer(store)
        other = await _peer(store, "other")
        arguments = {
            "peer_id": str(peer["id"]),
            "tenant": "research",
            "message_id": "message-1",
            "task_id": None,
            "context_id": None,
            "reference_task_ids": [],
            "request_fingerprint": "sha256:first",
            "message": {"messageId": "message-1", "role": "ROLE_USER", "parts": []},
            **_ids("first"),
        }
        task, message, created = await store.prepare_message(**arguments)
        replay_task, replay_message, replay_created = await store.prepare_message(**arguments)

        assert created is True
        assert replay_created is False
        assert replay_task == task
        assert replay_message == message
        assert message["message"]["taskId"] == task["id"]
        assert message["message"]["contextId"] == task["context_id"]
        assert (
            await store.get_task(peer_id=str(other["id"]), tenant="other", task_id=str(task["id"]))
            is None
        )

        with pytest.raises(A2AMessageConflictError):
            await store.prepare_message(**{**arguments, "request_fingerprint": "sha256:different"})
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_message_transactions_are_serialized(tmp_path: Path) -> None:
    store = await A2AGatewayStore.open(tmp_path / "gateway.db")
    try:
        peer = await _peer(store)
        peer_id = str(peer["id"])

        async def prepare(index: int) -> tuple[dict[str, object], dict[str, object], bool]:
            message_id = f"message-{index}"
            return await store.prepare_message(
                peer_id=peer_id,
                tenant="research",
                message_id=message_id,
                task_id=None,
                context_id=None,
                reference_task_ids=[],
                request_fingerprint=f"sha256:{index}",
                message={"messageId": message_id, "role": "ROLE_USER", "parts": []},
                **_ids(message_id),
            )

        results = await asyncio.gather(*(prepare(index) for index in range(8)))

        assert len({str(task["id"]) for task, _message, _created in results}) == 8
        assert all(created for _task, _message, created in results)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_push_config_ids_are_scoped_to_their_task(tmp_path: Path) -> None:
    store = await A2AGatewayStore.open(tmp_path / "gateway.db")
    try:
        peer = await _peer(store)
        peer_id = str(peer["id"])
        tasks: list[dict[str, object]] = []
        for index in range(2):
            message_id = f"push-message-{index}"
            task, _message, _created = await store.prepare_message(
                peer_id=peer_id,
                tenant="research",
                message_id=message_id,
                task_id=None,
                context_id=None,
                reference_task_ids=[],
                request_fingerprint=f"sha256:push-{index}",
                message={"messageId": message_id, "role": "ROLE_USER", "parts": []},
                **_ids(message_id),
            )
            tasks.append(task)
            config, created = await store.create_push_config(
                config_id="shared-config-id",
                peer_id=peer_id,
                tenant="research",
                task_id=str(task["id"]),
                request_fingerprint=f"sha256:config-{index}",
                url="https://callback.example.test/events",
                token_ciphertext=None,
                auth_scheme=None,
                credentials_ciphertext=None,
                start_after=0,
                snapshot_payload={"task": {"id": str(task["id"])}},
            )
            assert created is True
            assert config["id"] == "shared-config-id"

        assert await store.delete_push_config(
            peer_id=peer_id,
            tenant="research",
            task_id=str(tasks[0]["id"]),
            config_id="shared-config-id",
        )
        remaining = await store.get_push_config(
            peer_id=peer_id,
            tenant="research",
            task_id=str(tasks[1]["id"]),
            config_id="shared-config-id",
        )
        assert remaining is not None
        delivery = await store.claim_push_delivery()
        assert delivery is not None
        assert delivery["task_id"] == tasks[1]["id"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_followup_infers_context_and_context_only_message_starts_new_task(
    tmp_path: Path,
) -> None:
    store = await A2AGatewayStore.open(tmp_path / "gateway.db")
    try:
        peer = await _peer(store)
        peer_id = str(peer["id"])
        first, _message, _created = await store.prepare_message(
            peer_id=peer_id,
            tenant="research",
            message_id="message-1",
            task_id=None,
            context_id=None,
            reference_task_ids=[],
            request_fingerprint="sha256:first",
            message={"messageId": "message-1", "role": "ROLE_USER", "parts": []},
            **_ids("first"),
        )
        await store.settle_task_admission(
            peer_id=peer_id,
            task_id=str(first["id"]),
            runtime_run_id="run-first",
        )
        followup, followup_message, created = await store.prepare_message(
            peer_id=peer_id,
            tenant="research",
            message_id="message-2",
            task_id=str(first["id"]),
            context_id=None,
            reference_task_ids=[],
            request_fingerprint="sha256:second",
            message={"messageId": "message-2", "role": "ROLE_USER", "parts": []},
            **_ids("second"),
        )
        assert created is False
        assert followup["id"] == first["id"]
        assert followup_message["context_id"] == first["context_id"]

        second, _context_message, created = await store.prepare_message(
            peer_id=peer_id,
            tenant="research",
            message_id="message-3",
            task_id=None,
            context_id=str(first["context_id"]),
            reference_task_ids=[str(first["id"])],
            request_fingerprint="sha256:third",
            message={"messageId": "message-3", "role": "ROLE_USER", "parts": []},
            **_ids("third"),
        )
        assert created is True
        assert second["id"] != first["id"]
        assert second["context_id"] == first["context_id"]
        assert second["runtime_thread_id"] != first["runtime_thread_id"]

        with pytest.raises(ValueError, match="context_id"):
            await store.prepare_message(
                peer_id=peer_id,
                tenant="research",
                message_id="message-4",
                task_id=str(first["id"]),
                context_id="ctx-wrong",
                reference_task_ids=[],
                request_fingerprint="sha256:fourth",
                message={"messageId": "message-4", "role": "ROLE_USER", "parts": []},
                **_ids("fourth"),
            )
    finally:
        await store.close()
