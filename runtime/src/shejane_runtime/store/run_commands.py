"""Immutable command receipts and command-driven run transitions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from ..auth import LOCAL_OWNER_PRINCIPAL_ID
from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import (
    CommandConflictError,
    ParentRunAdmissionError,
    RunAdmissionError,
    ThreadAdmissionError,
    WorkspaceAdmissionError,
)
from .events import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from .ids import new_id as _new_id


def _principal_thread_id(principal_id: str, requested_id: str) -> str:
    if principal_id == LOCAL_OWNER_PRINCIPAL_ID and not requested_id.startswith("pt_"):
        return requested_id
    principal_prefix = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:16]
    prefix = f"pt_{principal_prefix}_"
    if requested_id.startswith(prefix):
        return requested_id
    logical_hash = hashlib.sha256(requested_id.encode("utf-8")).hexdigest()
    return f"{prefix}{logical_hash}"


def _principal_item_id(principal_id: str, requested_id: str) -> str:
    if principal_id == LOCAL_OWNER_PRINCIPAL_ID and not requested_id.startswith("pi_"):
        return requested_id
    principal_prefix = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:16]
    prefix = f"pi_{principal_prefix}_"
    if requested_id.startswith(prefix):
        return requested_id
    logical_hash = hashlib.sha256(requested_id.encode("utf-8")).hexdigest()
    return f"{prefix}{logical_hash}"


class RunCommandStore(SqliteDatabase):
    @staticmethod
    async def _accepted_run_for_command(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        command_id: str,
        payload_json: str,
    ) -> dict[str, Any] | None:
        command = await (
            await conn.execute(
                "SELECT payload_json, run_id FROM local_commands WHERE principal_id = ? AND id = ?",
                (principal_id, command_id),
            )
        ).fetchone()
        if command is None:
            return None
        if command["payload_json"] != payload_json:
            raise CommandConflictError(
                f"command {command_id} was already accepted with different content"
            )
        run = await (
            await conn.execute(
                "SELECT r.*, c.id AS command_id, c.client_message_id "
                "FROM local_runs r JOIN local_commands c ON c.run_id = r.id "
                "AND c.principal_id = r.principal_id "
                "WHERE r.id = ? AND c.principal_id = ? AND c.id = ?",
                (command["run_id"], principal_id, command_id),
            )
        ).fetchone()
        if run is None:
            raise RuntimeError(f"command {command_id} references a missing run")
        return dict(run)

    @staticmethod
    async def _accepted_command_receipt_uncommitted(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload_json: str,
    ) -> dict[str, Any] | None:
        existing = await (
            await conn.execute(
                "SELECT command_type, payload_json, response_json "
                "FROM local_commands WHERE principal_id = ? AND id = ?",
                (principal_id, command_id),
            )
        ).fetchone()
        if existing is None:
            return None
        if existing["command_type"] != command_type or existing["payload_json"] != payload_json:
            raise CommandConflictError(
                f"command {command_id} was already accepted with different content"
            )
        return json.loads(existing["response_json"])

    async def accepted_command_receipt(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._accepted_command_receipt_uncommitted(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            command_type=command_type,
            payload_json=_encode_payload(payload),
        )

    async def record_command_receipt(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload_json = _encode_payload(payload)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        now,
                    ),
                )
                await conn.commit()
                return receipt
            except BaseException:
                await conn.rollback()
                raise

    async def accepted_run_for_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        client_message_id: str,
        command_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return an immutable command receipt before checking mutable resources."""
        return await self._accepted_run_for_command(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            payload_json=_encode_payload(
                {"client_message_id": client_message_id, "payload": command_payload}
            ),
        )

    async def accept_run_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        client_message_id: str,
        command_payload: dict[str, Any],
        goal: str,
        workspace_path: str | None,
        mode: str,
        thread_id: str | None = None,
        user_input: str | None = None,
        assistant_message_id: str | None = None,
        thread_title: str | None = None,
        thread_metadata: dict[str, Any] | None = None,
        user_item_metadata: dict[str, Any] | None = None,
        replace_from_client_id: str | None = None,
        require_new_thread: bool = False,
        graph_thread_id: str | None = None,
        graph_checkpoint_id: str | None = None,
        graph_definition_id: str | None = None,
        graph_input_kind: str = "new",
        history: list[dict[str, str]] | None = None,
        parent_run_id: str | None = None,
        settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        admission_error: RunAdmissionError | None = None,
        plugin_refs: list[dict[str, Any]] | None = None,
        plugin_command: dict[str, Any] | None = None,
        inherit_plugin_bindings_from: str | None = None,
        run_inputs: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one immutable start command and its queued run."""
        payload_json = _encode_payload(
            {"client_message_id": client_message_id, "payload": command_payload}
        )
        existing = await self._accepted_run_for_command(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            payload_json=payload_json,
        )
        if existing is not None:
            return existing, False
        path_error = (
            await self._workspace_path_error(workspace_path) if admission_error is None else None
        )
        if path_error is not None:
            existing = await self._accepted_run_for_command(
                self._conn,
                principal_id=principal_id,
                command_id=command_id,
                payload_json=payload_json,
            )
            if existing is not None:
                return existing, False
            raise WorkspaceAdmissionError(path_error)

        async with aiosqlite.connect(str(self._db_path)) as transaction_conn:
            await _configure_connection(transaction_conn)
            await transaction_conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_run_for_command(
                    transaction_conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await transaction_conn.rollback()
                    return existing, False
                if admission_error is not None:
                    raise admission_error
                if path_error is not None:
                    raise WorkspaceAdmissionError(path_error)

                workspace_error = await self._workspace_owner_error(
                    transaction_conn,
                    principal_id=principal_id,
                    path=workspace_path,
                )
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                admitted_root_run_id: str | None = None
                if parent_run_id is not None:
                    parent = await (
                        await transaction_conn.execute(
                            "SELECT status, run_kind, root_run_id FROM local_runs "
                            "WHERE principal_id = ? AND id = ?",
                            (principal_id, parent_run_id),
                        )
                    ).fetchone()
                    if parent is None:
                        raise ParentRunAdmissionError("parent run not found")
                    if str(parent["run_kind"]) == "child":
                        raise ParentRunAdmissionError(
                            "a durable child run cannot be used as a conversation parent"
                        )
                    if str(parent["status"]) in {"queued", "running", "cleanup_required"}:
                        raise ParentRunAdmissionError(
                            "parent run has not reached a safely settled state"
                        )
                    admitted_root_run_id = str(parent["root_run_id"] or parent_run_id)

                plugin_bindings = await self._resolve_run_plugin_bindings(
                    transaction_conn,
                    principal_id=principal_id,
                    plugin_refs=plugin_refs or [],
                    plugin_command=plugin_command,
                    inherit_from_run_id=inherit_plugin_bindings_from,
                )
                normalized_user_item_metadata = dict(user_item_metadata or {})
                normalized_user_item_metadata.pop("plugin_selection", None)
                if plugin_refs or plugin_command:
                    bindings_by_id = {
                        str(binding["plugin_id"]): binding for binding in plugin_bindings
                    }
                    references = []
                    seen_references: set[str] = set()
                    for reference in plugin_refs or []:
                        plugin_id = str(reference["plugin_id"])
                        if plugin_id in seen_references:
                            continue
                        binding = bindings_by_id[plugin_id]
                        references.append(
                            {
                                "plugin_id": plugin_id,
                                "name": str(binding["display_name"]),
                                "digest": str(binding["digest"]),
                            }
                        )
                        seen_references.add(plugin_id)
                    selection: dict[str, Any] = {"references": references}
                    if plugin_command is not None:
                        plugin_id = str(plugin_command["plugin_id"])
                        binding = bindings_by_id[plugin_id]
                        selection["command"] = {
                            "plugin_id": plugin_id,
                            "plugin_name": str(binding["display_name"]),
                            "command_id": str(plugin_command["command_id"]),
                            "title": str(binding["command_title"]),
                            "digest": str(binding["digest"]),
                        }
                    normalized_user_item_metadata["plugin_selection"] = selection

                product_thread_id = (
                    _principal_thread_id(principal_id, thread_id)
                    if thread_id is not None
                    else _new_id("thread")
                )
                thread = await (
                    await transaction_conn.execute(
                        "SELECT * FROM local_threads WHERE id = ?",
                        (product_thread_id,),
                    )
                ).fetchone()
                now = _now()
                seed_history: list[dict[str, str]] = []
                if thread is not None and require_new_thread:
                    raise ThreadAdmissionError("fork target thread already exists")
                initial_thread_title: str | None = None
                if thread is None:
                    if replace_from_client_id is not None:
                        raise ThreadAdmissionError("thread not found")
                    initial_thread_title = " ".join((thread_title or user_input or goal).split())[
                        :80
                    ]
                    await transaction_conn.execute(
                        "INSERT INTO local_threads "
                        "(id, principal_id, title, metadata_json, version, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, ?, ?)",
                        (
                            product_thread_id,
                            principal_id,
                            initial_thread_title,
                            _encode_payload(thread_metadata or {}),
                            now,
                            now,
                        ),
                    )
                    thread_version = 1
                    seed_history = [
                        {"role": str(message["role"]), "content": str(message["content"])}
                        for message in history or []
                        if message.get("role") in {"user", "assistant"}
                        and str(message.get("content") or "").strip()
                    ]
                    base_position = len(seed_history)
                else:
                    if thread["principal_id"] != principal_id:
                        raise ThreadAdmissionError("thread not found")
                    if thread["deleted_at"] is not None:
                        raise ThreadAdmissionError("thread not found")
                    if thread["archived_at"] is not None:
                        raise ThreadAdmissionError("thread is archived")
                    duplicate_item = await (
                        await transaction_conn.execute(
                            "SELECT 1 FROM local_thread_items "
                            "WHERE thread_id = ? AND client_id = ?",
                            (product_thread_id, client_message_id),
                        )
                    ).fetchone()
                    if duplicate_item is not None:
                        raise CommandConflictError(
                            f"client message {client_message_id} already belongs to another command"
                        )
                    active = await (
                        await transaction_conn.execute(
                            "SELECT 1 FROM local_runs r "
                            "LEFT JOIN local_run_jobs j ON j.run_id = r.id "
                            "WHERE r.principal_id = ? AND r.thread_id = ? AND ("
                            "r.status NOT IN ('completed', 'failed', 'canceled') "
                            "OR j.status IN ('pending', 'leased')) LIMIT 1",
                            (principal_id, product_thread_id),
                        )
                    ).fetchone()
                    if active is not None:
                        raise ThreadAdmissionError("thread has an unsettled run")
                    if replace_from_client_id is not None:
                        target = await (
                            await transaction_conn.execute(
                                "SELECT position FROM local_thread_items "
                                "WHERE thread_id = ? AND client_id = ? "
                                "AND item_type = 'user_message' AND superseded_at IS NULL",
                                (product_thread_id, replace_from_client_id),
                            )
                        ).fetchone()
                        if target is None:
                            raise ThreadAdmissionError("thread message not found")
                        base_position = int(target["position"]) - 1
                    else:
                        position_row = await (
                            await transaction_conn.execute(
                                "SELECT COALESCE(MAX(position), 0) FROM local_thread_items "
                                "WHERE thread_id = ? AND superseded_at IS NULL",
                                (product_thread_id,),
                            )
                        ).fetchone()
                        base_position = int(position_row[0] if position_row else 0)
                    thread_version = int(thread["version"]) + 1
                    await transaction_conn.execute(
                        "UPDATE local_threads SET version = ?, title = ?, metadata_json = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            thread_version,
                            " ".join((thread_title or thread["title"]).split())[:80],
                            _encode_payload(thread_metadata)
                            if thread_metadata is not None
                            else thread["metadata_json"],
                            now,
                            product_thread_id,
                        ),
                    )

                if thread is None:
                    authoritative_history = seed_history
                    if seed_history:
                        await transaction_conn.executemany(
                            "INSERT INTO local_thread_items "
                            "(id, thread_id, run_id, client_id, item_type, status, content, "
                            "metadata_json, position, version, created_at, updated_at, completed_at) "
                            "VALUES (?, ?, NULL, NULL, ?, 'completed', ?, '{}', ?, 1, ?, ?, ?)",
                            [
                                (
                                    _new_id("item"),
                                    product_thread_id,
                                    f"{message['role']}_message",
                                    message["content"],
                                    position,
                                    now,
                                    now,
                                    now,
                                )
                                for position, message in enumerate(seed_history, start=1)
                            ],
                        )
                else:
                    history_rows = await (
                        await transaction_conn.execute(
                            "SELECT item_type, content FROM local_thread_items "
                            "WHERE thread_id = ? AND superseded_at IS NULL "
                            "AND position <= ? AND item_type IN "
                            "('user_message', 'assistant_message') AND content != '' "
                            "ORDER BY position, id",
                            (product_thread_id, base_position),
                        )
                    ).fetchall()
                    authoritative_history = [
                        {
                            "role": "user" if row["item_type"] == "user_message" else "assistant",
                            "content": str(row["content"]),
                        }
                        for row in history_rows
                    ]

                assistant_item_id = (
                    _principal_item_id(principal_id, assistant_message_id)
                    if assistant_message_id is not None
                    else _new_id("item")
                )
                run = self._new_run_record(
                    principal_id=principal_id,
                    goal=goal,
                    workspace_path=workspace_path,
                    parent_run_id=parent_run_id,
                    settings=settings,
                    metadata=metadata,
                    mode=mode,
                    history=authoritative_history,
                    thread_id=product_thread_id,
                    assistant_item_id=assistant_item_id,
                    user_input=user_input,
                    graph_thread_id=graph_thread_id,
                    graph_checkpoint_id=graph_checkpoint_id,
                    graph_definition_id=graph_definition_id,
                    graph_input_kind=graph_input_kind,
                    run_kind="fork" if graph_input_kind == "fork" else "turn",
                    root_run_id=admitted_root_run_id,
                )
                await self._insert_run(transaction_conn, run)
                if run_inputs:
                    await transaction_conn.executemany(
                        "INSERT INTO local_run_inputs "
                        "(run_id, input_id, virtual_path, original_name, media_type, bytes, "
                        "sha256, blob_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                run["id"],
                                item["input_id"],
                                item["virtual_path"],
                                item["original_name"],
                                item["media_type"],
                                item["bytes"],
                                item["sha256"],
                                item["blob_key"],
                                now,
                            )
                            for item in run_inputs
                        ],
                    )
                if plugin_bindings:
                    await transaction_conn.executemany(
                        "INSERT INTO run_plugin_bindings "
                        "(run_id, plugin_id, version, digest, selection_source, required, "
                        "command_id, action_catalog_hash, model_binding_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                run["id"],
                                item["plugin_id"],
                                item["version"],
                                item["digest"],
                                item["selection_source"],
                                item["required"],
                                item["command_id"],
                                item["action_catalog_hash"],
                                item.get("model_binding_json"),
                            )
                            for item in plugin_bindings
                        ],
                    )
                if replace_from_client_id is not None:
                    await transaction_conn.execute(
                        "UPDATE local_thread_items SET superseded_at = ?, "
                        "superseded_by_run_id = ?, updated_at = ? "
                        "WHERE thread_id = ? AND position > ? AND superseded_at IS NULL",
                        (now, run["id"], now, product_thread_id, base_position),
                    )
                await transaction_conn.executemany(
                    "INSERT INTO local_thread_items "
                    "(id, thread_id, run_id, client_id, item_type, status, content, "
                    "metadata_json, position, version, created_at, updated_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        (
                            _new_id("item"),
                            product_thread_id,
                            run["id"],
                            client_message_id,
                            "user_message",
                            "completed",
                            user_input or goal,
                            _encode_payload(normalized_user_item_metadata),
                            base_position + 1,
                            now,
                            now,
                            now,
                        ),
                        (
                            assistant_item_id,
                            product_thread_id,
                            run["id"],
                            assistant_message_id,
                            "assistant_message",
                            "in_progress",
                            "",
                            "{}",
                            base_position + 2,
                            now,
                            now,
                            None,
                        ),
                    ),
                )
                await transaction_conn.execute(
                    "INSERT INTO local_thread_changes "
                    "(principal_id, thread_id, thread_version, change_type, run_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        principal_id,
                        product_thread_id,
                        thread_version,
                        "thread.rewritten"
                        if replace_from_client_id is not None
                        else "turn.started",
                        run["id"],
                        now,
                    ),
                )
                await self._insert_run_job(
                    transaction_conn,
                    self._new_run_job_record(
                        run_id=run["id"],
                        kind="start",
                        input_payload={
                            "principal_id": principal_id,
                            "goal": goal,
                            "user_input": user_input or goal,
                            "workspace_path": workspace_path,
                            "mode": mode,
                            "history": authoritative_history,
                            "settings": settings or {},
                            "metadata": metadata or {},
                        },
                    ),
                )
                await transaction_conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        str(command_payload.get("type") or "run.start"),
                        client_message_id,
                        payload_json,
                        _encode_payload(run),
                        run["id"],
                        run["created_at"],
                    ),
                )
                await transaction_conn.commit()
                return {
                    **run,
                    "command_id": command_id,
                    "client_message_id": client_message_id,
                }, True
            except BaseException:
                await transaction_conn.rollback()
                raise

    async def request_run_cancel(self, run_id: str) -> str | None:
        states = await self.request_run_cancel_tree(run_id)
        return states.get(run_id)

    async def request_run_cancel_tree(self, run_id: str) -> dict[str, str]:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                states = await self._request_run_cancel_tree_uncommitted(conn, run_id)
                if (
                    not states
                    and await (
                        await conn.execute("SELECT 1 FROM local_runs WHERE id = ?", (run_id,))
                    ).fetchone()
                    is None
                ):
                    await conn.rollback()
                    return {}
                await conn.commit()
                return states
            except BaseException:
                await conn.rollback()
                raise

    async def request_run_cancel_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload({"type": "run.cancel", "run_id": run_id})
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="run.cancel",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                run = await (
                    await conn.execute(
                        "SELECT id, created_at FROM local_runs WHERE principal_id = ? AND id = ?",
                        (principal_id, run_id),
                    )
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown run: {run_id}")
                states = await self._request_run_cancel_tree_uncommitted(conn, run_id)
                receipt = {
                    "type": "run.cancel",
                    "command_id": command_id,
                    "run_id": run_id,
                    "canceled": run_id in states,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, 'run.cancel', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        _now(),
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def _request_run_cancel_tree_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
    ) -> dict[str, str]:
        rows = await (
            await conn.execute(
                "WITH RECURSIVE tree(id, depth) AS ("
                "SELECT id, 0 FROM local_runs WHERE id = ? UNION ALL "
                "SELECT r.id, tree.depth + 1 FROM local_runs r "
                "JOIN tree ON r.parent_run_id = tree.id WHERE r.run_kind = 'child'"
                ") SELECT id FROM tree ORDER BY depth DESC, id",
                (run_id,),
            )
        ).fetchall()
        states: dict[str, str] = {}
        for row in rows:
            target_run_id = str(row["id"])
            state = await self._request_run_cancel_uncommitted(conn, target_run_id)
            if state is not None:
                states[target_run_id] = state
        return states

    async def _request_run_cancel_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
    ) -> str | None:
        row = await (
            await conn.execute(
                "SELECT * FROM local_run_jobs WHERE run_id = ? "
                "AND status IN ('pending', 'leased') AND quarantined_at IS NULL",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            run = await (
                await conn.execute(
                    "SELECT status FROM local_runs WHERE id = ?",
                    (run_id,),
                )
            ).fetchone()
            if run is None or run["status"] not in {"waiting_permission", "waiting_input"}:
                return None
            requested_at = _now()
            await self._finish_run_cancel_uncommitted(
                conn,
                run_id=run_id,
                requested_at=requested_at,
            )
            return "waiting"
        job = dict(row)
        requested_at = _now()
        if job["status"] == "leased":
            await conn.execute(
                "UPDATE local_run_jobs SET cancel_requested_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased'",
                (requested_at, requested_at, job["id"]),
            )
            return "leased"

        await conn.execute(
            "UPDATE local_run_jobs SET status = 'canceled', cancel_requested_at = ?, "
            "updated_at = ?, finished_at = ? WHERE id = ? AND status = 'pending'",
            (requested_at, requested_at, requested_at, job["id"]),
        )
        await self._finish_run_cancel_uncommitted(
            conn,
            run_id=run_id,
            requested_at=requested_at,
        )
        return "pending"

    async def _finish_run_cancel_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        requested_at: str,
    ) -> None:
        cancel_decision = _encode_payload({"type": "cancel", "reason": "run_canceled"})
        await conn.execute(
            "UPDATE local_permissions SET status = 'canceled', decision_json = ?, "
            "resolved_at = ? WHERE run_id = ? AND status = 'pending'",
            (cancel_decision, requested_at, run_id),
        )
        await conn.execute(
            "UPDATE local_questions SET status = 'canceled' "
            "WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
        await conn.execute(
            "UPDATE local_wait_candidates SET status = 'resolved', decision_json = ?, "
            "resolved_at = ? WHERE run_id = ? AND status = 'pending'",
            (cancel_decision, requested_at, run_id),
        )
        await conn.execute(
            "UPDATE local_plan_approvals SET status = 'canceled', resolved_at = ? "
            "WHERE run_id = ? AND status = 'pending'",
            (requested_at, run_id),
        )
        await self._cancel_unstarted_tool_receipts_uncommitted(
            conn,
            run_id=run_id,
            canceled_at=requested_at,
        )
        await conn.execute(
            "UPDATE local_runs SET status = 'canceled', updated_at = ?, completed_at = ? "
            "WHERE id = ?",
            (requested_at, requested_at, run_id),
        )
        event = await self._append_event_uncommitted(
            conn,
            run_id,
            "run.canceled",
            payload_json="{}",
            created_at=requested_at,
        )
        await self._update_thread_projection_uncommitted(
            conn,
            run_id=run_id,
            run_status="canceled",
            change_type="run.canceled",
            payload={},
            event_high_watermark=int(event["seq"]),
            changed_at=requested_at,
        )

    async def request_run_inject_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        run_id: str,
        content: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload({"type": "run.inject", "run_id": run_id, "content": content})
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="run.inject",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                run = await (
                    await conn.execute(
                        "SELECT id, status FROM local_runs WHERE principal_id = ? AND id = ?",
                        (principal_id, run_id),
                    )
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown run: {run_id}")
                if str(run["status"]) in _TERMINAL_RUN_STATUSES | {"cleanup_required"}:
                    raise RunAdmissionError("run_not_active", "run is not active")

                now = _now()
                instruction_id = _new_id("steer")
                await conn.execute(
                    "INSERT INTO local_steering "
                    "(id, run_id, content, status, created_at, injected_at) "
                    "VALUES (?, ?, ?, 'pending', ?, NULL)",
                    (instruction_id, run_id, content, now),
                )
                receipt = {
                    "command_id": command_id,
                    "run_id": run_id,
                    "instruction_id": instruction_id,
                    "queued": True,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'run.inject', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise
