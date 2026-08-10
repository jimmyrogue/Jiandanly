"""Durable child Run topology and root collaboration projections."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import aiosqlite

from .agent_messages import _agent_message_projection
from .codec import json_payload as _json_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import RunAdmissionError


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


class CollaborationProjectionStore(SqliteDatabase):
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
