"""Fair shared/exclusive tool execution gate with ordered batch turns."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class AsyncToolExecutionGate:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self._batch_next: dict[str, int] = {}

    @asynccontextmanager
    async def read(self):
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self):
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()

    @asynccontextmanager
    async def ordered(self, batch_key: str, position: int, completed_prefix: int = 0):
        async with self._condition:
            current = self._batch_next.get(batch_key, 0)
            if completed_prefix > current:
                self._batch_next[batch_key] = completed_prefix
            await self._condition.wait_for(lambda: self._batch_next.get(batch_key, 0) == position)
        completed = False
        try:
            yield
            completed = True
        finally:
            if completed:
                async with self._condition:
                    self._batch_next[batch_key] = position + 1
                    self._condition.notify_all()
