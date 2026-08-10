from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .store.sqlite import LocalStore


async def _owned_run(
    store: LocalStore,
    *,
    principal_id: str,
    run_id: str,
    not_found_detail: str = "run not found",
) -> dict[str, Any]:
    run = await store.get_run_for_principal(principal_id=principal_id, run_id=run_id)
    if run is None:
        detail: str | dict[str, str] = not_found_detail
        if not_found_detail == "run not found":
            detail = {"code": "run_not_found", "message": not_found_detail}
        raise HTTPException(status_code=404, detail=detail)
    return run


async def _run_with_inputs(store: LocalStore, run: dict[str, Any]) -> dict[str, Any]:
    return (await _runs_with_inputs(store, [run]))[0]


async def _runs_with_inputs(store: LocalStore, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run_ids = [str(run["id"]) for run in runs]
    missing_subagent_ids = [str(run["id"]) for run in runs if "subagent_invocations" not in run]
    rows, subagent_rows, child_rows = await asyncio.gather(
        store.list_run_inputs_for_runs(run_ids),
        store.list_subagent_invocations_for_runs(missing_subagent_ids),
        store.list_child_runs_for_runs(run_ids),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["run_id"]), []).append(row)
    grouped_subagents: dict[str, list[dict[str, Any]]] = {}
    for row in subagent_rows:
        grouped_subagents.setdefault(str(row["parent_run_id"]), []).append(row)
    grouped_children: dict[str, list[dict[str, Any]]] = {}
    for row in child_rows:
        grouped_children.setdefault(str(row["parent_run_id"]), []).append(row)
    return [
        {
            **run,
            "inputs": [
                {
                    "client_index": index,
                    **{
                        key: item[key]
                        for key in (
                            "input_id",
                            "virtual_path",
                            "original_name",
                            "media_type",
                            "bytes",
                            "sha256",
                        )
                    },
                }
                for index, item in enumerate(grouped.get(str(run["id"]), []))
            ],
            "subagent_invocations": run.get(
                "subagent_invocations",
                grouped_subagents.get(str(run["id"]), []),
            ),
            "child_runs": run.get(
                "child_runs",
                grouped_children.get(str(run["id"]), []),
            ),
        }
        for run in runs
    ]


async def _normalized_path(raw: str) -> str:
    return await asyncio.to_thread(
        lambda: str(Path(os.path.abspath(os.path.expanduser(raw))).resolve())
    )


async def _authorized_workspace_path(
    store: LocalStore, *, principal_id: str, path: str | None
) -> str | None:
    if path is None:
        return None
    resolved = await _normalized_path(path)
    workspace = await store.workspace_by_path(principal_id=principal_id, path=resolved)
    if workspace is None:
        raise HTTPException(status_code=403, detail="workspace is not authorized")
    workspace_error = await store.workspace_admission_error(
        principal_id=principal_id,
        path=resolved,
    )
    if workspace_error is not None:
        raise HTTPException(status_code=409, detail=workspace_error)
    return resolved
