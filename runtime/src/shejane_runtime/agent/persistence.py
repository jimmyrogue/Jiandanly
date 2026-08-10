"""Long-lived LangGraph checkpoint and Store resources."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore
from langgraph.store.sqlite.aio import AsyncSqliteStore

from ..config import Settings, get_settings

log = logging.getLogger("shejane_runtime.agent.builder")


async def open_checkpointer(
    settings: Settings | None = None,
) -> tuple[AsyncSqliteSaver, AsyncExitStack]:
    """Open a long-lived AsyncSqliteSaver.

    Eager `await checkpointer.setup()` avoids the lazy-init disk-I/O race
    observed in the Phase 0 spike on macOS APFS.
    """
    settings = settings or get_settings()
    settings.ensure_data_dir()
    stack = AsyncExitStack()
    saver = await stack.enter_async_context(
        AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db_path))
    )
    await saver.setup()
    log.info("checkpointer ready at %s", settings.checkpoint_db_path)
    return saver, stack


async def open_store(settings: Settings | None = None) -> tuple[BaseStore, AsyncExitStack]:
    """Open a long-lived `BaseStore` for cross-run durable memory.

    This is what explicit memory tools write into and what
    `langgraph.store.base.BaseStore`-aware tools read via `runtime.store`.

    Backed by `AsyncSqliteStore` on the runtime data dir — same WAL +
    eager-setup pattern as the checkpointer.
    """
    settings = settings or get_settings()
    settings.ensure_data_dir()
    stack = AsyncExitStack()
    store = await stack.enter_async_context(
        AsyncSqliteStore.from_conn_string(str(settings.store_db_path))
    )
    await store.setup()
    log.info("store ready at %s", settings.store_db_path)
    return store, stack
