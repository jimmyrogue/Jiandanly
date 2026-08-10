"""Coordinate model-connection mutation with admitted and active Runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RunModelConnectionCoordinationMixin:
    async def cancel_model_connection_runs(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> int:
        """Cancel active runs before mutating a model connection."""
        run_ids: list[str] = []
        for run_id, settings in list(self._settings_overrides.items()):
            if run_id not in self._tasks:
                continue
            binding = settings.get("_model_binding")
            capability_bindings = settings.get("_capability_bindings")
            uses_connection = (
                isinstance(binding, dict) and binding.get("connection_id") == connection_id
            ) or (
                isinstance(capability_bindings, dict)
                and any(
                    isinstance(item, dict) and item.get("connection_id") == connection_id
                    for item in capability_bindings.values()
                )
            )
            if not uses_connection:
                continue
            run = await self.store.get_run(run_id)
            if run is not None and run.get("principal_id") == principal_id:
                run_ids.append(run_id)
        tasks = [self._tasks[run_id] for run_id in run_ids if run_id in self._tasks]
        for run_id in run_ids:
            canceled = await self.cancel_run(run_id)
            if not canceled:
                task = self._tasks.get(run_id)
                if task is not None:
                    task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=5.0)
            if pending:
                raise RuntimeError("active model connection runs did not stop")
        return len(run_ids)

    def _model_connection_lock(self, principal_id: str, connection_id: str) -> asyncio.Lock:
        return self._model_bindings.connection_lock(principal_id, connection_id)

    @asynccontextmanager
    async def model_connection_mutation(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> AsyncIterator[None]:
        """Fence admission and execution while a model connection changes."""
        async with self._model_connection_lock(principal_id, connection_id):
            await self.cancel_model_connection_runs(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            yield

    @asynccontextmanager
    async def model_connection_catalog_update(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> AsyncIterator[None]:
        """Serialize catalog writes with admission without canceling Runs."""
        async with self._model_connection_lock(principal_id, connection_id):
            yield
