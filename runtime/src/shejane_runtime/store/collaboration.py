"""Durable child Run coordination and same-root Agent mailboxes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .database import CURRENT_EXECUTION_LEASE as _CURRENT_EXECUTION_LEASE
from .database import LeaseFenceError, SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import CommandConflictError, RunAdmissionError, ToolReceiptStateError
from .events import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from .ids import new_id as _new_id

MAX_DURABLE_CHILD_DEPTH = 1
MAX_DURABLE_CHILDREN_PER_RUN = 8
MAX_DURABLE_CHILD_DEPENDENCIES = MAX_DURABLE_CHILDREN_PER_RUN - 1
MAX_DURABLE_CHILD_RESOURCE_CLAIMS = 16
MAX_AGENT_MAILBOX_PENDING = 32
MAX_AGENT_MAILBOX_MESSAGES_PER_ROOT = 512
MAX_AGENT_MAILBOX_HOPS = 8
MAX_AGENT_MAILBOX_ARTIFACT_REFS = 16
MAX_AGENT_MAILBOX_TEXT_BYTES = 32 * 1024
MAX_AGENT_MAILBOX_DATA_BYTES = 16 * 1024
_CHILD_EVENT_BY_RUN_EVENT = {
    "run.started": ("child.started", "running"),
    "run.resumed": ("child.started", "running"),
    "run.waiting": ("child.waiting", None),
    "run.completed": ("child.completed", "completed"),
    "run.failed": ("child.failed", "failed"),
    "run.canceled": ("child.canceled", "canceled"),
    "run.cleanup_required": ("child.cleanup_required", "cleanup_required"),
}


def _normalize_workspace_resource_key(workspace_path: str | None, requested_path: str) -> str:
    if not workspace_path:
        raise RunAdmissionError(
            "child_resource_workspace_required",
            "resource ownership requires an authorized workspace",
        )
    raw = str(requested_path).strip()
    if not raw or "\x00" in raw:
        raise ValueError("collaboration resource path is invalid")
    workspace = Path(workspace_path).resolve(strict=False)
    candidate = Path(raw)
    if candidate.is_absolute():
        absolute = candidate.resolve(strict=False)
        resolved = (
            absolute
            if absolute.is_relative_to(workspace)
            else (workspace / raw.lstrip("/\\")).resolve(strict=False)
        )
    else:
        resolved = (workspace / candidate).resolve(strict=False)
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise RunAdmissionError(
            "child_resource_outside_workspace",
            "collaboration resource path must stay inside the authorized workspace",
        ) from exc
    key = PurePosixPath(relative.as_posix()).as_posix()
    if key in {"", "."}:
        raise ValueError("collaboration resource must identify a file inside the workspace")
    if len(key.encode("utf-8")) > 4096:
        raise ValueError("collaboration resource path is too long")
    return key


def _normalize_child_coordination(
    coordination: dict[str, Any] | None,
    *,
    workspace_path: str | None,
) -> dict[str, Any]:
    raw = dict(coordination or {})
    unknown = set(raw) - {
        "completion_mode",
        "depends_on",
        "resource_claims",
        "quorum_group",
        "quorum_required",
    }
    if unknown:
        raise ValueError(f"unknown child coordination fields: {', '.join(sorted(unknown))}")

    completion_mode = str(raw.get("completion_mode") or "required").strip()
    if completion_mode not in {"required", "best_effort", "quorum"}:
        raise ValueError("child completion_mode is invalid")

    depends_raw = raw.get("depends_on") or []
    if isinstance(depends_raw, (str, bytes)) or not isinstance(depends_raw, Sequence):
        raise ValueError("child depends_on must be a list")
    depends_on = [str(run_id).strip() for run_id in depends_raw]
    if (
        len(depends_on) > MAX_DURABLE_CHILD_DEPENDENCIES
        or len(depends_on) != len(set(depends_on))
        or any(not run_id or len(run_id) > 128 for run_id in depends_on)
    ):
        raise ValueError("child depends_on is invalid")

    claims_raw = raw.get("resource_claims") or []
    if isinstance(claims_raw, (str, bytes)) or not isinstance(claims_raw, Sequence):
        raise ValueError("child resource_claims must be a list")
    resource_claims = [
        _normalize_workspace_resource_key(workspace_path, str(path)) for path in claims_raw
    ]
    if len(resource_claims) > MAX_DURABLE_CHILD_RESOURCE_CLAIMS or len(resource_claims) != len(
        set(resource_claims)
    ):
        raise ValueError("child resource_claims is invalid")

    quorum_group_raw = raw.get("quorum_group")
    quorum_required_raw = raw.get("quorum_required")
    if completion_mode == "quorum":
        quorum_group = str(quorum_group_raw or "").strip()
        if not quorum_group or len(quorum_group) > 128:
            raise ValueError("quorum children require a quorum_group")
        if isinstance(quorum_required_raw, bool) or not isinstance(quorum_required_raw, int):
            raise ValueError("quorum children require quorum_required")
        if not 1 <= quorum_required_raw <= MAX_DURABLE_CHILDREN_PER_RUN:
            raise ValueError("child quorum_required is invalid")
        quorum_required: int | None = quorum_required_raw
    else:
        if quorum_group_raw is not None or quorum_required_raw is not None:
            raise ValueError("quorum fields are only valid for quorum children")
        quorum_group = None
        quorum_required = None

    return {
        "completion_mode": completion_mode,
        "depends_on": depends_on,
        "resource_claims": resource_claims,
        "quorum_group": quorum_group,
        "quorum_required": quorum_required,
    }


def _agent_message_projection(row: dict[str, Any]) -> dict[str, Any]:
    projected = dict(row)
    projected["data"] = _json_payload(projected.pop("data_json", None))
    try:
        artifact_refs = json.loads(str(projected.pop("artifact_refs_json", "[]")))
    except (json.JSONDecodeError, TypeError):
        artifact_refs = []
    projected["artifact_refs"] = artifact_refs if isinstance(artifact_refs, list) else []
    projected["sequence"] = int(projected["sequence"])
    projected["hop_count"] = int(projected["hop_count"])
    projected["ttl_seconds"] = int(projected["ttl_seconds"])
    return projected


def _normalize_agent_message_content(
    *,
    kind: str,
    text: str,
    data: dict[str, Any],
    artifact_refs: Sequence[str],
    ttl_seconds: int,
) -> tuple[str, str, str, str, int]:
    normalized_kind = str(kind).strip().lower()
    if normalized_kind not in {"request", "question", "update", "result", "cancel"}:
        raise ValueError("Agent message kind is invalid")
    normalized_text = str(text).strip()
    if len(normalized_text.encode("utf-8")) > MAX_AGENT_MAILBOX_TEXT_BYTES:
        raise ValueError("Agent message text is too large")
    if not isinstance(data, dict):
        raise ValueError("Agent message data must be an object")
    try:
        data_json = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent message data must be JSON-compatible") from exc
    if len(data_json.encode("utf-8")) > MAX_AGENT_MAILBOX_DATA_BYTES:
        raise ValueError("Agent message data is too large")
    if isinstance(artifact_refs, (str, bytes)):
        raise ValueError("Agent message artifact_refs must be a list")
    normalized_refs = list(dict.fromkeys(str(ref).strip() for ref in artifact_refs))
    if len(normalized_refs) > MAX_AGENT_MAILBOX_ARTIFACT_REFS or any(
        not ref or len(ref) > 512 for ref in normalized_refs
    ):
        raise ValueError("Agent message artifact_refs are invalid")
    if not normalized_text and not data and not normalized_refs:
        raise ValueError("Agent message content is empty")
    if isinstance(ttl_seconds, bool) or not 60 <= int(ttl_seconds) <= 24 * 60 * 60:
        raise ValueError("Agent message ttl_seconds must be between 60 and 86400")
    return (
        normalized_kind,
        normalized_text,
        data_json,
        _encode_payload(normalized_refs),
        int(ttl_seconds),
    )


def _normalize_child_agent_definition(value: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "id",
        "version",
        "name",
        "description",
        "system_prompt",
        "allowed_tools",
    }
    if set(value) != allowed_keys:
        raise ValueError("child Agent definition has invalid fields")
    normalized: dict[str, Any] = {}
    for key, maximum in (
        ("id", 128),
        ("version", 128),
        ("name", 64),
        ("description", 4096),
        ("system_prompt", 32 * 1024),
    ):
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.strip() or len(raw) > maximum:
            raise ValueError(f"child Agent definition {key} is invalid")
        normalized[key] = raw.strip()
    raw_tools = value.get("allowed_tools")
    if (
        not isinstance(raw_tools, list)
        or len(raw_tools) > 128
        or any(not isinstance(name, str) or not name or len(name) > 128 for name in raw_tools)
    ):
        raise ValueError("child Agent definition allowed_tools is invalid")
    normalized["allowed_tools"] = sorted(set(raw_tools))
    if len(_encode_payload(normalized).encode("utf-8")) > 64 * 1024:
        raise ValueError("child Agent definition is too large")
    return normalized


class CollaborationStore(SqliteDatabase):
    async def accept_child_run(
        self,
        *,
        parent_run_id: str,
        spawn_operation_id: str,
        goal: str,
        agent_definition: dict[str, Any],
        coordination: dict[str, Any] | None = None,
        execution_policy: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically admit one Runtime-owned child Run and pending Job.

        The caller must be executing the matching ``child.spawn`` Tool Receipt
        under the parent's current job lease. ``spawn_operation_id`` is the
        replay key, so a process crash can rediscover the same child instead of
        creating a duplicate.
        """
        normalized_goal = goal.strip()
        if not normalized_goal or len(normalized_goal) > 32 * 1024:
            raise ValueError("child task is invalid")
        definition = _normalize_child_agent_definition(agent_definition)
        definition_json = _encode_payload(definition)
        async with self.run_write_transaction(parent_run_id) as conn:
            parent_row = await (
                await conn.execute("SELECT * FROM local_runs WHERE id = ?", (parent_run_id,))
            ).fetchone()
            if parent_row is None:
                raise KeyError(f"unknown parent run: {parent_run_id}")
            parent = dict(parent_row)
            normalized_coordination = _normalize_child_coordination(
                coordination,
                workspace_path=parent.get("workspace_path"),
            )
            policy = {
                "max_depth": MAX_DURABLE_CHILD_DEPTH,
                "max_children": MAX_DURABLE_CHILDREN_PER_RUN,
                **normalized_coordination,
            }
            policy_json = _encode_payload(policy)
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_runs WHERE spawn_operation_id = ?",
                    (spawn_operation_id,),
                )
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                metadata = _json_payload(record.get("metadata_json"))
                if (
                    record.get("run_kind") != "child"
                    or record.get("parent_run_id") != parent_run_id
                    or record.get("goal") != normalized_goal
                    or record.get("agent_definition_id") != definition["id"]
                    or record.get("agent_definition_version") != definition["version"]
                    or _encode_payload(metadata.get("_child_agent_definition") or {})
                    != definition_json
                    or _encode_payload(_json_payload(record.get("collaboration_policy_json")))
                    != policy_json
                ):
                    raise CommandConflictError(
                        f"spawn operation {spawn_operation_id} was reused with a different child specification"
                    )
                return record, False

            receipt = await (
                await conn.execute(
                    "SELECT tool_name, status, execution_attempt_id FROM local_tool_receipts "
                    "WHERE operation_id = ? AND run_id = ?",
                    (spawn_operation_id, parent_run_id),
                )
            ).fetchone()
            if (
                receipt is None
                or receipt["tool_name"] != "child.spawn"
                or receipt["status"] != "running"
            ):
                raise ToolReceiptStateError(
                    "child admission requires its running child.spawn receipt"
                )
            lease = _CURRENT_EXECUTION_LEASE.get()
            if lease is None:
                raise LeaseFenceError("child admission requires the parent execution lease")
            expected_attempt_id = f"{lease.job_id}:{lease.lease_generation}"
            if str(receipt["execution_attempt_id"]) != expected_attempt_id:
                raise LeaseFenceError("child.spawn receipt belongs to a stale execution attempt")
            if parent["status"] != "running":
                raise RunAdmissionError(
                    "child_parent_not_running",
                    "a child can only be admitted while its parent is running",
                )
            child_depth = int(parent.get("collaboration_depth") or 0) + 1
            if child_depth > MAX_DURABLE_CHILD_DEPTH:
                raise RunAdmissionError(
                    "child_depth_exceeded",
                    "durable child depth is exhausted for this Run",
                )
            child_count = int(
                (
                    await (
                        await conn.execute(
                            "SELECT COUNT(*) FROM local_runs "
                            "WHERE parent_run_id = ? AND run_kind = 'child'",
                            (parent_run_id,),
                        )
                    ).fetchone()
                )[0]
            )
            if child_count >= MAX_DURABLE_CHILDREN_PER_RUN:
                raise RunAdmissionError(
                    "child_fanout_exceeded",
                    "durable child fan-out is exhausted for this Run",
                )

            depends_on = normalized_coordination["depends_on"]
            if depends_on:
                placeholders = ",".join("?" for _ in depends_on)
                rows = await (
                    await conn.execute(
                        "SELECT id FROM local_runs WHERE parent_run_id = ? "
                        f"AND run_kind = 'child' AND id IN ({placeholders})",
                        (parent_run_id, *depends_on),
                    )
                ).fetchall()
                found = {str(row["id"]) for row in rows}
                missing = [run_id for run_id in depends_on if run_id not in found]
                if missing:
                    raise RunAdmissionError(
                        "child_dependency_invalid",
                        "child dependencies must be previously admitted siblings",
                    )

            quorum_group = normalized_coordination["quorum_group"]
            quorum_required = normalized_coordination["quorum_required"]
            if quorum_group is not None:
                existing_quorum = await (
                    await conn.execute(
                        "SELECT quorum_required FROM local_child_coordination "
                        "WHERE parent_run_id = ? AND completion_mode = 'quorum' "
                        "AND quorum_group = ? LIMIT 1",
                        (parent_run_id, quorum_group),
                    )
                ).fetchone()
                if existing_quorum is not None and int(existing_quorum[0]) != quorum_required:
                    raise RunAdmissionError(
                        "child_quorum_conflict",
                        "all children in a quorum group must use the same quorum_required",
                    )

            root_run_id = str(parent.get("root_run_id") or parent_run_id)
            for resource_key in normalized_coordination["resource_claims"]:
                owner = await (
                    await conn.execute(
                        "SELECT owner_run_id FROM local_collaboration_resource_claims "
                        "WHERE root_run_id = ? AND resource_key = ?",
                        (root_run_id, resource_key),
                    )
                ).fetchone()
                if owner is not None:
                    raise RunAdmissionError(
                        "child_resource_already_owned",
                        f"collaboration resource is already owned by {owner['owner_run_id']}",
                    )

            parent_metadata = _json_payload(parent.get("metadata_json"))
            child_metadata: dict[str, Any] = {
                "_child_agent_definition": definition,
                "_spawn_operation_id": spawn_operation_id,
            }
            attachments = parent_metadata.get("_attachments")
            if isinstance(attachments, list):
                child_metadata["_attachments"] = attachments
            child_settings = _json_payload(parent.get("settings_json"))
            child_settings.pop("_execution_policy", None)
            if execution_policy is not None:
                child_settings["_execution_policy"] = dict(execution_policy)
            child = self._new_run_record(
                principal_id=str(parent["principal_id"]),
                goal=normalized_goal,
                workspace_path=parent.get("workspace_path"),
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                settings=child_settings,
                metadata=child_metadata,
                mode=str(parent["mode"]),
                run_kind="child",
                agent_definition_id=str(definition["id"]),
                agent_definition_version=str(definition["version"]),
                collaboration_depth=child_depth,
                collaboration_policy=policy,
                spawn_operation_id=spawn_operation_id,
            )
            await self._insert_run(conn, child)
            await conn.execute(
                "INSERT INTO local_child_coordination "
                "(child_run_id, root_run_id, parent_run_id, completion_mode, quorum_group, "
                "quorum_required, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    child["id"],
                    root_run_id,
                    parent_run_id,
                    normalized_coordination["completion_mode"],
                    quorum_group,
                    quorum_required,
                    child["created_at"],
                ),
            )
            for dependency_run_id in depends_on:
                await conn.execute(
                    "INSERT INTO local_child_dependencies "
                    "(child_run_id, dependency_run_id, created_at) VALUES (?, ?, ?)",
                    (child["id"], dependency_run_id, child["created_at"]),
                )
            for resource_key in normalized_coordination["resource_claims"]:
                await conn.execute(
                    "INSERT INTO local_collaboration_resource_claims "
                    "(root_run_id, resource_key, owner_run_id, created_at) VALUES (?, ?, ?, ?)",
                    (root_run_id, resource_key, child["id"], child["created_at"]),
                )
            await conn.execute(
                "INSERT INTO local_run_inputs "
                "(run_id, input_id, virtual_path, original_name, media_type, bytes, "
                "sha256, blob_key, created_at) "
                "SELECT ?, input_id, virtual_path, original_name, media_type, bytes, "
                "sha256, blob_key, created_at FROM local_run_inputs WHERE run_id = ?",
                (child["id"], parent_run_id),
            )
            await conn.execute(
                "INSERT INTO run_plugin_bindings "
                "(run_id, plugin_id, version, digest, selection_source, required, command_id, "
                "action_catalog_hash, model_binding_json) "
                "SELECT ?, plugin_id, version, digest, selection_source, required, command_id, "
                "action_catalog_hash, model_binding_json FROM run_plugin_bindings WHERE run_id = ?",
                (child["id"], parent_run_id),
            )
            await self._insert_run_job(
                conn,
                self._new_run_job_record(
                    run_id=str(child["id"]),
                    kind="start",
                    input_payload=self._run_job_input(child),
                ),
            )
            spawned_payload = {
                "child_run_id": str(child["id"]),
                "parent_run_id": parent_run_id,
                "root_run_id": str(child["root_run_id"]),
                "agent_definition_id": str(child["agent_definition_id"]),
                "agent_definition_version": str(child["agent_definition_version"]),
                "collaboration_depth": child_depth,
                "goal": normalized_goal,
                "status": "queued",
                "spawn_operation_id": spawn_operation_id,
                **normalized_coordination,
                "created_at": str(child["created_at"]),
                "updated_at": str(child["updated_at"]),
            }
            event = await self._append_event_uncommitted(
                conn,
                parent_run_id,
                "child.spawned",
                payload_json=_encode_payload(spawned_payload),
                created_at=str(child["created_at"]),
            )
            await self._touch_thread_for_run_event_uncommitted(
                conn,
                run_id=parent_run_id,
                change_type="child.spawned",
                event_high_watermark=int(event["seq"]),
                changed_at=str(child["created_at"]),
            )
            return {**child, "_spawn_event": event}, True

    async def list_child_runs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    "r.parent_run_id = ? AND r.run_kind = 'child'",
                    "ORDER BY r.created_at, r.id",
                ),
                (run_id,),
            )
        ).fetchall()
        return [self._child_run_projection(dict(row)) for row in rows]

    async def child_run_for_spawn_operation(
        self,
        parent_run_id: str,
        spawn_operation_id: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    "r.parent_run_id = ? AND r.spawn_operation_id = ? AND r.run_kind = 'child'",
                ),
                (parent_run_id, spawn_operation_id),
            )
        ).fetchone()
        return self._child_run_projection(dict(row)) if row is not None else None

    async def list_child_runs_for_runs(
        self,
        parent_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(run_id) for run_id in parent_run_ids if run_id))
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        rows = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    f"r.parent_run_id IN ({placeholders}) AND r.run_kind = 'child'",
                    "ORDER BY r.parent_run_id, r.created_at, r.id",
                ),
                normalized_ids,
            )
        ).fetchall()
        return [self._child_run_projection(dict(row)) for row in rows]

    async def child_runs_for_parent(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(run_id) for run_id in child_run_ids if run_id))
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        rows = await (
            await self._conn.execute(
                self._child_run_snapshot_sql(
                    f"r.parent_run_id = ? AND r.run_kind = 'child' AND r.id IN ({placeholders})",
                ),
                (parent_run_id, *normalized_ids),
            )
        ).fetchall()
        by_id = {str(row["id"]): self._child_run_projection(dict(row)) for row in rows}
        missing = [run_id for run_id in normalized_ids if run_id not in by_id]
        if missing:
            raise KeyError(f"child run not found: {missing[0]}")
        return [by_id[run_id] for run_id in normalized_ids]

    @staticmethod
    def _child_run_snapshot_sql(where: str, suffix: str = "") -> str:
        return (
            "SELECT r.id, r.parent_run_id, r.root_run_id, r.run_kind, r.goal, r.status, "
            "r.agent_definition_id, r.agent_definition_version, r.collaboration_depth, "
            "r.collaboration_policy_json, r.spawn_operation_id, r.graph_thread_id, "
            "r.graph_checkpoint_id, r.created_at, r.updated_at, r.completed_at, "
            "COALESCE(c.completion_mode, 'required') AS completion_mode, "
            "c.quorum_group, c.quorum_required, "
            "COALESCE((SELECT json_group_array(dependency_run_id) FROM "
            "(SELECT dependency_run_id FROM local_child_dependencies "
            "WHERE child_run_id = r.id ORDER BY dependency_run_id)), '[]') AS depends_on_json, "
            "COALESCE((SELECT json_group_array(resource_key) FROM "
            "(SELECT resource_key FROM local_collaboration_resource_claims "
            "WHERE owner_run_id = r.id ORDER BY resource_key)), '[]') AS resource_claims_json, "
            "d.content AS result_text, "
            "(SELECT e.payload_json FROM local_events e WHERE e.run_id = r.id "
            "AND e.event_type IN ('run.waiting', 'run.completed', 'run.failed', "
            "'run.canceled', 'run.cleanup_required') ORDER BY e.seq DESC LIMIT 1) "
            "AS result_payload_json, "
            "(SELECT COUNT(*) FROM local_events e WHERE e.run_id = r.id) AS events_count "
            "FROM local_runs r LEFT JOIN local_assistant_drafts d ON d.run_id = r.id "
            "LEFT JOIN local_child_coordination c ON c.child_run_id = r.id "
            f"WHERE {where} {suffix}"
        )

    @staticmethod
    def _child_run_projection(row: dict[str, Any]) -> dict[str, Any]:
        payload = _json_payload(row.pop("result_payload_json", None))
        result_text = row.pop("result_text", None)
        collaboration_policy = _json_payload(row.pop("collaboration_policy_json", None))
        try:
            depends_on = json.loads(str(row.pop("depends_on_json", "[]")))
            resource_claims = json.loads(str(row.pop("resource_claims_json", "[]")))
        except (json.JSONDecodeError, TypeError):
            depends_on = []
            resource_claims = []
        projected = {
            **row,
            "collaboration_policy": collaboration_policy,
            "depends_on": depends_on if isinstance(depends_on, list) else [],
            "resource_claims": resource_claims if isinstance(resource_claims, list) else [],
            "quorum_required": (
                int(row["quorum_required"]) if row.get("quorum_required") is not None else None
            ),
            "result": (
                str(result_text)
                if row.get("status") == "completed" and result_text is not None
                else None
            ),
            "error": payload.get("error"),
            "error_type": payload.get("type"),
            "retryable": payload.get("retryable"),
            "input_tokens": int(payload.get("input_tokens") or 0),
            "output_tokens": int(payload.get("output_tokens") or 0),
            "model_calls": int(payload.get("model_calls") or 0),
        }
        return projected

    async def assert_workspace_resource_owner(
        self,
        *,
        run_id: str,
        requested_path: str,
    ) -> None:
        run = await (
            await self._conn.execute(
                "SELECT root_run_id, workspace_path FROM local_runs WHERE id = ?",
                (run_id,),
            )
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        root_run_id = str(run["root_run_id"] or run_id)
        has_claims = await (
            await self._conn.execute(
                "SELECT 1 FROM local_collaboration_resource_claims WHERE root_run_id = ? LIMIT 1",
                (root_run_id,),
            )
        ).fetchone()
        if has_claims is None:
            return
        resource_key = _normalize_workspace_resource_key(
            str(run["workspace_path"]) if run["workspace_path"] else None,
            requested_path,
        )
        owner = await (
            await self._conn.execute(
                "SELECT owner_run_id FROM local_collaboration_resource_claims "
                "WHERE root_run_id = ? AND resource_key = ?",
                (root_run_id, resource_key),
            )
        ).fetchone()
        if owner is not None and str(owner["owner_run_id"]) != run_id:
            raise RunAdmissionError(
                "collaboration_resource_not_owned",
                f"workspace resource {resource_key} is owned by another collaboration member",
            )

    async def has_foreign_workspace_resource_claims(self, run_id: str) -> bool:
        row = await (
            await self._conn.execute(
                "SELECT 1 FROM local_collaboration_resource_claims claims "
                "JOIN local_runs run ON run.root_run_id = claims.root_run_id "
                "WHERE run.id = ? AND claims.owner_run_id != ? LIMIT 1",
                (run_id, run_id),
            )
        ).fetchone()
        return row is not None

    async def collaboration_snapshot(self, root_run_id: str) -> dict[str, Any]:
        """Read one root collaboration at a single SQLite snapshot boundary."""
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            try:
                root_row = await (
                    await conn.execute(
                        "SELECT r.id, r.parent_run_id, r.root_run_id, r.run_kind, r.goal, "
                        "r.status, r.agent_definition_id, r.agent_definition_version, "
                        "r.graph_thread_id, r.graph_checkpoint_id, r.created_at, r.updated_at, "
                        "r.completed_at, d.content AS result_text, "
                        "(SELECT e.payload_json FROM local_events e WHERE e.run_id = r.id "
                        "AND e.event_type IN ('run.waiting', 'run.completed', 'run.failed', "
                        "'run.canceled', 'run.cleanup_required') "
                        "ORDER BY e.seq DESC LIMIT 1) AS result_payload_json "
                        "FROM local_runs r LEFT JOIN local_assistant_drafts d ON d.run_id = r.id "
                        "WHERE r.id = ?",
                        (root_run_id,),
                    )
                ).fetchone()
                if root_row is None:
                    raise KeyError(f"unknown run: {root_run_id}")
                if str(root_row["root_run_id"] or root_row["id"]) != root_run_id:
                    raise RunAdmissionError(
                        "collaboration_root_required",
                        "collaboration snapshots must be requested from the root Run",
                    )

                child_rows = await (
                    await conn.execute(
                        self._child_run_snapshot_sql(
                            "r.parent_run_id = ? AND r.run_kind = 'child'",
                            "ORDER BY r.created_at, r.id",
                        ),
                        (root_run_id,),
                    )
                ).fetchall()
                children = [self._child_run_projection(dict(row)) for row in child_rows]
                run_ids = [root_run_id, *(str(child["id"]) for child in children)]
                placeholders = ",".join("?" for _ in run_ids)

                cursor_rows = await (
                    await conn.execute(
                        f"SELECT run_id, COALESCE(MAX(seq), 0) AS high_watermark "
                        f"FROM local_events WHERE run_id IN ({placeholders}) GROUP BY run_id",
                        run_ids,
                    )
                ).fetchall()
                event_high_watermarks = {run_id: 0 for run_id in run_ids}
                event_high_watermarks.update(
                    {str(row["run_id"]): int(row["high_watermark"]) for row in cursor_rows}
                )

                message_rows = await (
                    await conn.execute(
                        "SELECT * FROM local_agent_messages WHERE root_run_id = ? "
                        "ORDER BY created_at, id",
                        (root_run_id,),
                    )
                ).fetchall()
                wait_rows = await (
                    await conn.execute(
                        f"SELECT * FROM local_wait_candidates WHERE run_id IN ({placeholders}) "
                        "AND status = 'pending' ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()
                resource_rows = await (
                    await conn.execute(
                        "SELECT resource_key, owner_run_id, created_at "
                        "FROM local_collaboration_resource_claims WHERE root_run_id = ? "
                        "ORDER BY resource_key, owner_run_id",
                        (root_run_id,),
                    )
                ).fetchall()
                dependency_rows = await (
                    await conn.execute(
                        f"SELECT child_run_id, dependency_run_id FROM local_child_dependencies "
                        f"WHERE child_run_id IN ({placeholders}) "
                        "ORDER BY child_run_id, dependency_run_id",
                        run_ids,
                    )
                ).fetchall()
                artifact_rows = await (
                    await conn.execute(
                        f"SELECT id, run_id, kind, title, content_type, bytes, sha256, "
                        f"storage_kind, tool_name, created_at FROM local_artifacts "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()

                root = dict(root_row)
                root_payload = _json_payload(root.pop("result_payload_json", None))
                root_result_text = root.pop("result_text", None)
                root.update(
                    result=(
                        str(root_result_text)
                        if root.get("status") == "completed" and root_result_text is not None
                        else None
                    ),
                    error=root_payload.get("error"),
                    error_type=root_payload.get("type"),
                    retryable=root_payload.get("retryable"),
                    input_tokens=int(root_payload.get("input_tokens") or 0),
                    output_tokens=int(root_payload.get("output_tokens") or 0),
                    model_calls=int(root_payload.get("model_calls") or 0),
                )
                pending_waits: list[dict[str, Any]] = []
                for row in wait_rows:
                    wait = dict(row)
                    wait["payload"] = _json_payload(wait.pop("payload_json", None))
                    decision_raw = wait.pop("decision_json", None)
                    wait["decision"] = _json_payload(decision_raw) if decision_raw else None
                    pending_waits.append(wait)
                captured_at = _now()
                await conn.commit()
                return {
                    "schema_version": 1,
                    "captured_at": captured_at,
                    "root": root,
                    "children": children,
                    "messages": [_agent_message_projection(dict(row)) for row in message_rows],
                    "pending_waits": pending_waits,
                    "resource_owners": [dict(row) for row in resource_rows],
                    "dependencies": [dict(row) for row in dependency_rows],
                    "artifacts": [dict(row) for row in artifact_rows],
                    "event_high_watermarks": event_high_watermarks,
                }
            except BaseException:
                await conn.rollback()
                raise

    @staticmethod
    async def _require_mailbox_receipt_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        tool_name: str,
        operation_id: str | None,
    ) -> None:
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != run_id:
            raise LeaseFenceError("Agent mailbox mutation requires the sender execution lease")
        execution_attempt_id = f"{lease.job_id}:{lease.lease_generation}"
        if operation_id is None:
            rows = await (
                await conn.execute(
                    "SELECT operation_id FROM local_tool_receipts WHERE run_id = ? "
                    "AND tool_name = ? AND status = 'running' AND execution_attempt_id = ?",
                    (run_id, tool_name, execution_attempt_id),
                )
            ).fetchall()
            if len(rows) != 1:
                raise ToolReceiptStateError(
                    f"Agent mailbox mutation requires one running {tool_name} receipt"
                )
            return
        receipt = await (
            await conn.execute(
                "SELECT tool_name, status, execution_attempt_id FROM local_tool_receipts "
                "WHERE operation_id = ? AND run_id = ?",
                (operation_id, run_id),
            )
        ).fetchone()
        if (
            receipt is None
            or str(receipt["tool_name"]) != tool_name
            or str(receipt["status"]) != "running"
        ):
            raise ToolReceiptStateError(
                f"Agent mailbox mutation requires its running {tool_name} receipt"
            )
        if str(receipt["execution_attempt_id"]) != execution_attempt_id:
            raise LeaseFenceError(f"{tool_name} receipt belongs to a stale execution attempt")

    async def _append_agent_message_event_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        event_type: str,
        message: dict[str, Any],
        created_at: str,
    ) -> None:
        event = await self._append_event_uncommitted(
            conn,
            run_id,
            event_type,
            payload_json=_encode_payload(_agent_message_projection(message)),
            created_at=created_at,
        )
        await self._touch_thread_for_run_event_uncommitted(
            conn,
            run_id=run_id,
            change_type=event_type,
            event_high_watermark=int(event["seq"]),
            changed_at=created_at,
        )

    async def _expire_agent_messages_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        recipient_run_id: str,
        now: str,
    ) -> None:
        expired = await (
            await conn.execute(
                "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                "AND status IN ('queued', 'delivered') AND deadline_at <= ? "
                "ORDER BY created_at, id",
                (recipient_run_id, now),
            )
        ).fetchall()
        if not expired:
            return
        await conn.execute(
            "UPDATE local_agent_messages SET status = 'expired' WHERE recipient_run_id = ? "
            "AND status IN ('queued', 'delivered') AND deadline_at <= ?",
            (recipient_run_id, now),
        )
        for row in expired:
            message = {**dict(row), "status": "expired"}
            await self._append_agent_message_event_uncommitted(
                conn,
                run_id=recipient_run_id,
                event_type="agent.message.expired",
                message=message,
                created_at=now,
            )

    async def _insert_agent_message_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: str,
        text: str,
        data_json: str,
        artifact_refs_json: str,
        ttl_seconds: int,
        correlation_id: str | None,
        in_reply_to: str | None,
        sequence: int,
        hop_count: int,
        tool_name: str,
    ) -> tuple[dict[str, Any], bool]:
        existing = await (
            await conn.execute(
                "SELECT * FROM local_agent_messages WHERE sender_operation_id = ?",
                (sender_operation_id,),
            )
        ).fetchone()
        if existing is not None:
            record = dict(existing)
            expected_correlation = correlation_id or str(record["id"])
            if any(
                (str(record[key]) if record[key] is not None else None)
                != (str(value) if value is not None else None)
                for key, value in (
                    ("sender_run_id", sender_run_id),
                    ("recipient_run_id", recipient_run_id),
                    ("kind", kind),
                    ("text", text),
                    ("data_json", data_json),
                    ("artifact_refs_json", artifact_refs_json),
                    ("ttl_seconds", ttl_seconds),
                    ("correlation_id", expected_correlation),
                    ("in_reply_to", in_reply_to),
                    ("sequence", sequence),
                    ("hop_count", hop_count),
                )
            ):
                raise CommandConflictError(
                    f"mailbox operation {sender_operation_id} was reused with a different message"
                )
            return _agent_message_projection(record), False

        await self._require_mailbox_receipt_uncommitted(
            conn,
            run_id=sender_run_id,
            tool_name=tool_name,
            operation_id=sender_operation_id,
        )
        if sender_run_id == recipient_run_id:
            raise RunAdmissionError(
                "agent_message_self_send",
                "an Agent cannot send a mailbox message to itself",
            )
        rows = await (
            await conn.execute(
                "SELECT id, principal_id, run_kind, root_run_id, parent_run_id, "
                "collaboration_depth, status FROM local_runs WHERE id IN (?, ?)",
                (sender_run_id, recipient_run_id),
            )
        ).fetchall()
        by_id = {str(row["id"]): dict(row) for row in rows}
        if sender_run_id not in by_id or recipient_run_id not in by_id:
            raise RunAdmissionError(
                "agent_message_run_not_found",
                "Agent mailbox sender or recipient does not exist",
            )
        sender = by_id[sender_run_id]
        recipient = by_id[recipient_run_id]
        sender_root = str(sender.get("root_run_id") or sender_run_id)
        recipient_root = str(recipient.get("root_run_id") or recipient_run_id)
        if sender_root != recipient_root:
            raise RunAdmissionError(
                "agent_message_foreign_root",
                "Agent mailbox participants must share one collaboration root",
            )
        if sender["principal_id"] != recipient["principal_id"]:
            raise RunAdmissionError(
                "agent_message_foreign_principal",
                "Agent mailbox participants must share one principal",
            )
        if sender["run_kind"] != "child" and recipient["run_kind"] != "child":
            raise RunAdmissionError(
                "agent_message_requires_child",
                "Agent mailbox messages require a durable child participant",
            )
        for participant in (sender, recipient):
            if participant["run_kind"] == "child" and (
                str(participant.get("parent_run_id") or "") != sender_root
                or int(participant.get("collaboration_depth") or 0) != 1
            ):
                raise RunAdmissionError(
                    "agent_message_invalid_topology",
                    "Agent mailbox participants must belong to the direct-child topology",
                )
        artifact_refs = json.loads(artifact_refs_json)
        if artifact_refs:
            placeholders = ",".join("?" for _ in artifact_refs)
            authorized_artifacts = await (
                await conn.execute(
                    "SELECT a.id FROM local_artifacts a JOIN local_runs r ON r.id = a.run_id "
                    f"WHERE a.id IN ({placeholders}) AND COALESCE(r.root_run_id, r.id) = ?",
                    (*artifact_refs, sender_root),
                )
            ).fetchall()
            authorized_ids = {str(row["id"]) for row in authorized_artifacts}
            if any(ref not in authorized_ids for ref in artifact_refs):
                raise RunAdmissionError(
                    "agent_message_artifact_forbidden",
                    "Agent mailbox artifact references must belong to the collaboration root",
                )
        if str(recipient["status"]) in _TERMINAL_RUN_STATUSES:
            raise RunAdmissionError(
                "agent_message_recipient_terminal",
                "Agent mailbox recipient is already terminal",
            )

        now = _now()
        await self._expire_agent_messages_uncommitted(
            conn,
            recipient_run_id=recipient_run_id,
            now=now,
        )
        pending = await (
            await conn.execute(
                "SELECT COUNT(*) FROM local_agent_messages WHERE recipient_run_id = ? "
                "AND status IN ('queued', 'delivered')",
                (recipient_run_id,),
            )
        ).fetchone()
        if int(pending[0]) >= MAX_AGENT_MAILBOX_PENDING:
            raise RunAdmissionError(
                "agent_message_backpressure",
                "Agent mailbox backpressure limit is reached",
            )
        root_total = await (
            await conn.execute(
                "SELECT COUNT(*) FROM local_agent_messages WHERE root_run_id = ?",
                (sender_root,),
            )
        ).fetchone()
        if int(root_total[0]) >= MAX_AGENT_MAILBOX_MESSAGES_PER_ROOT:
            raise RunAdmissionError(
                "agent_message_root_budget_exhausted",
                "Agent mailbox message budget is exhausted for this collaboration root",
            )

        message_id = _new_id("agent_message")
        record: dict[str, Any] = {
            "id": message_id,
            "root_run_id": sender_root,
            "sender_run_id": sender_run_id,
            "recipient_run_id": recipient_run_id,
            "sender_operation_id": sender_operation_id,
            "kind": kind,
            "text": text,
            "data_json": data_json,
            "artifact_refs_json": artifact_refs_json,
            "correlation_id": correlation_id or message_id,
            "in_reply_to": in_reply_to,
            "sequence": sequence,
            "hop_count": hop_count,
            "status": "queued",
            "ttl_seconds": ttl_seconds,
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
            "created_at": now,
            "delivered_at": None,
            "acknowledged_at": None,
        }
        await conn.execute(
            "INSERT INTO local_agent_messages "
            "(id, root_run_id, sender_run_id, recipient_run_id, sender_operation_id, kind, "
            "text, data_json, artifact_refs_json, correlation_id, in_reply_to, sequence, "
            "hop_count, status, ttl_seconds, deadline_at, created_at, delivered_at, "
            "acknowledged_at) VALUES (:id, :root_run_id, :sender_run_id, :recipient_run_id, "
            ":sender_operation_id, :kind, :text, :data_json, :artifact_refs_json, "
            ":correlation_id, :in_reply_to, :sequence, :hop_count, :status, :ttl_seconds, "
            ":deadline_at, :created_at, :delivered_at, :acknowledged_at)",
            record,
        )
        await self._append_agent_message_event_uncommitted(
            conn,
            run_id=sender_run_id,
            event_type="agent.message.sent",
            message=record,
            created_at=now,
        )
        return _agent_message_projection(record), True

    async def send_agent_message(
        self,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: str,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        normalized = _normalize_agent_message_content(
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        async with self.run_write_transaction(sender_run_id) as conn:
            return await self._insert_agent_message_uncommitted(
                conn,
                sender_run_id=sender_run_id,
                sender_operation_id=sender_operation_id,
                recipient_run_id=recipient_run_id,
                kind=normalized[0],
                text=normalized[1],
                data_json=normalized[2],
                artifact_refs_json=normalized[3],
                ttl_seconds=normalized[4],
                correlation_id=None,
                in_reply_to=None,
                sequence=1,
                hop_count=0,
                tool_name="mailbox.send",
            )

    async def reply_agent_message(
        self,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        in_reply_to: str,
        kind: str,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        normalized = _normalize_agent_message_content(
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        async with self.run_write_transaction(sender_run_id) as conn:
            replay = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE sender_operation_id = ?",
                    (sender_operation_id,),
                )
            ).fetchone()
            original = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE id = ?",
                    (in_reply_to,),
                )
            ).fetchone()
            if original is None:
                raise RunAdmissionError(
                    "agent_message_not_found",
                    "Agent mailbox reply target does not exist",
                )
            original_record = dict(original)
            if str(original_record["recipient_run_id"]) != sender_run_id:
                raise RunAdmissionError(
                    "agent_message_reply_forbidden",
                    "an Agent can only reply to a message addressed to it",
                )
            if str(original_record["kind"]) not in {"request", "question", "update"}:
                raise RunAdmissionError(
                    "agent_message_reply_terminal",
                    "result and cancel messages cannot be replied to",
                )
            if replay is None:
                thread_head = await (
                    await conn.execute(
                        "SELECT MAX(sequence), MAX(hop_count) FROM local_agent_messages "
                        "WHERE correlation_id = ?",
                        (original_record["correlation_id"],),
                    )
                ).fetchone()
                next_sequence = int(thread_head[0] or 0) + 1
                next_hop = int(thread_head[1] or 0) + 1
            else:
                next_sequence = int(replay["sequence"])
                next_hop = int(replay["hop_count"])
            if next_hop > MAX_AGENT_MAILBOX_HOPS:
                raise RunAdmissionError(
                    "agent_message_hop_limit",
                    "Agent mailbox conversation hop limit is exhausted",
                )
            return await self._insert_agent_message_uncommitted(
                conn,
                sender_run_id=sender_run_id,
                sender_operation_id=sender_operation_id,
                recipient_run_id=str(original_record["sender_run_id"]),
                kind=normalized[0],
                text=normalized[1],
                data_json=normalized[2],
                artifact_refs_json=normalized[3],
                ttl_seconds=normalized[4],
                correlation_id=str(original_record["correlation_id"]),
                in_reply_to=in_reply_to,
                sequence=next_sequence,
                hop_count=next_hop,
                tool_name="mailbox.reply",
            )

    async def deliver_agent_messages(self, recipient_run_id: str) -> list[dict[str, Any]]:
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != recipient_run_id:
            raise LeaseFenceError("Agent mailbox delivery requires the recipient execution lease")
        async with self.run_write_transaction(recipient_run_id) as conn:
            now = _now()
            await self._expire_agent_messages_uncommitted(
                conn,
                recipient_run_id=recipient_run_id,
                now=now,
            )
            queued = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                    "AND status = 'queued' ORDER BY created_at, id",
                    (recipient_run_id,),
                )
            ).fetchall()
            for row in queued:
                message = {**dict(row), "status": "delivered", "delivered_at": now}
                await conn.execute(
                    "UPDATE local_agent_messages SET status = 'delivered', delivered_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (now, row["id"]),
                )
                await self._append_agent_message_event_uncommitted(
                    conn,
                    run_id=recipient_run_id,
                    event_type="agent.message.received",
                    message=message,
                    created_at=now,
                )
            rows = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                    "AND status = 'delivered' ORDER BY created_at, id",
                    (recipient_run_id,),
                )
            ).fetchall()
            return [_agent_message_projection(dict(row)) for row in rows]

    async def ack_agent_messages(
        self,
        *,
        recipient_run_id: str,
        message_ids: Sequence[str],
        operation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(message_id) for message_id in message_ids))
        if not normalized_ids or len(normalized_ids) > MAX_AGENT_MAILBOX_PENDING:
            raise ValueError("Agent mailbox acknowledgement message_ids are invalid")
        async with self.run_write_transaction(recipient_run_id) as conn:
            await self._require_mailbox_receipt_uncommitted(
                conn,
                run_id=recipient_run_id,
                tool_name="mailbox.ack",
                operation_id=operation_id,
            )
            placeholders = ",".join("?" for _ in normalized_ids)
            rows = await (
                await conn.execute(
                    f"SELECT * FROM local_agent_messages WHERE id IN ({placeholders})",
                    normalized_ids,
                )
            ).fetchall()
            by_id = {str(row["id"]): dict(row) for row in rows}
            if any(message_id not in by_id for message_id in normalized_ids):
                raise RunAdmissionError(
                    "agent_message_not_found",
                    "Agent mailbox acknowledgement target does not exist",
                )
            now = _now()
            for message_id in normalized_ids:
                record = by_id[message_id]
                if str(record["recipient_run_id"]) != recipient_run_id:
                    raise RunAdmissionError(
                        "agent_message_ack_forbidden",
                        "an Agent can only acknowledge a message addressed to it",
                    )
                if str(record["status"]) == "acknowledged":
                    continue
                if str(record["status"]) != "delivered":
                    raise RunAdmissionError(
                        "agent_message_not_delivered",
                        "Agent mailbox message must be delivered before acknowledgement",
                    )
                await conn.execute(
                    "UPDATE local_agent_messages SET status = 'acknowledged', "
                    "acknowledged_at = ? WHERE id = ? AND status = 'delivered'",
                    (now, message_id),
                )
                record.update(status="acknowledged", acknowledged_at=now)
                await self._append_agent_message_event_uncommitted(
                    conn,
                    run_id=recipient_run_id,
                    event_type="agent.message.acknowledged",
                    message=record,
                    created_at=now,
                )
            return [_agent_message_projection(by_id[message_id]) for message_id in normalized_ids]

    async def list_agent_inbox(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                "ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [_agent_message_projection(dict(row)) for row in rows]

    async def list_agent_outbox(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_agent_messages WHERE sender_run_id = ? "
                "ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [_agent_message_projection(dict(row)) for row in rows]
