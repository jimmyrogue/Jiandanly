"""Stable A2A Gateway persistence facade and connection assembly."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .peer_store import A2APeerStore
from .peer_store import _normalize_push_origin as _normalize_push_origin
from .push_store import A2APushStore
from .store_common import A2AMessageConflictError as A2AMessageConflictError
from .store_common import A2APushConfigConflictError as A2APushConfigConflictError
from .store_schema import initialize_a2a_schema
from .task_store import A2ATaskStore


class A2AGatewayStore(A2APeerStore, A2ATaskStore, A2APushStore):
    def __init__(
        self,
        db_path: Path,
        connection: aiosqlite.Connection,
        transaction_connection: aiosqlite.Connection,
    ) -> None:
        self.db_path = db_path
        self._conn = connection
        self._transaction_conn = transaction_connection
        self._transaction_lock = asyncio.Lock()

    @classmethod
    async def open(cls, db_path: Path) -> A2AGatewayStore:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await initialize_a2a_schema(conn)
        transaction_conn = await aiosqlite.connect(str(path), isolation_level=None)
        transaction_conn.row_factory = aiosqlite.Row
        await transaction_conn.execute("PRAGMA busy_timeout=5000")
        await transaction_conn.execute("PRAGMA foreign_keys=ON")
        return cls(path, conn, transaction_conn)

    async def close(self) -> None:
        await self._transaction_conn.close()
        await self._conn.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._transaction_lock:
            await self._transaction_conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._transaction_conn
            except BaseException:
                await self._transaction_conn.rollback()
                raise
            else:
                await self._transaction_conn.commit()
