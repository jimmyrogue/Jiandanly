"""Atomic admission for durable child Runs and collaboration claims."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..codec import encode_payload as _encode_payload
from ..codec import json_payload as _json_payload
from ..database import CURRENT_EXECUTION_LEASE as _CURRENT_EXECUTION_LEASE
from ..database import LeaseFenceError
from ..errors import CommandConflictError, RunAdmissionError, ToolReceiptStateError
from .projection import (
    CollaborationProjectionStore,
    _normalize_workspace_resource_key,
)

MAX_DURABLE_CHILD_DEPTH = 1
MAX_DURABLE_CHILDREN_PER_RUN = 8
MAX_DURABLE_CHILD_DEPENDENCIES = MAX_DURABLE_CHILDREN_PER_RUN - 1
MAX_DURABLE_CHILD_RESOURCE_CLAIMS = 16
_CHILD_EVENT_BY_RUN_EVENT = {
    "run.started": ("child.started", "running"),
    "run.resumed": ("child.started", "running"),
    "run.waiting": ("child.waiting", None),
    "run.completed": ("child.completed", "completed"),
    "run.failed": ("child.failed", "failed"),
    "run.canceled": ("child.canceled", "canceled"),
    "run.cleanup_required": ("child.cleanup_required", "cleanup_required"),
}


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


class CollaborationStore(CollaborationProjectionStore):
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
