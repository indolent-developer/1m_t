"""Shared helpers for all repository implementations."""
from __future__ import annotations

from core.utils.log_helper import getLogger
from typing import Any

import asyncpg

from infrastructure.db.connection import get_pool

logger = getLogger(__name__)


class BaseRepository:
    async def _pool(self) -> asyncpg.Pool:
        return await get_pool()

    async def _execute(self, sql: str, *args: Any) -> None:
        pool = await self._pool()
        async with pool.acquire() as con:
            await con.execute(sql, *args)

    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        pool = await self._pool()
        async with pool.acquire() as con:
            return await con.fetch(sql, *args)

    async def _fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        pool = await self._pool()
        async with pool.acquire() as con:
            return await con.fetchrow(sql, *args)
