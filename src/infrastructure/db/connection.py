"""
infrastructure.db.connection

asyncpg connection pool + migration runner.

Usage:
    await apply_migrations()          # on startup, idempotent
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT * FROM equity_log LIMIT 10")

Config:
    DATABASE_URL env var, e.g.:
        postgresql://navalarya@localhost/trading
        postgresql://user:pass@host:5432/dbname
"""
from __future__ import annotations

from core.utils.log_helper import getLogger
import os
from pathlib import Path
from typing import Optional

import asyncpg

logger = getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise EnvironmentError(
                "DATABASE_URL is not set. "
                "Add it to .env, e.g. postgresql://user@localhost/trading"
            )
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=10)
        logger.info("[db] Pool connected → %s", url.split("@")[-1])
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("[db] Pool closed")


async def apply_migrations() -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                name       TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

        applied: set[str] = {
            r["name"] for r in await con.fetch("SELECT name FROM _migrations")
        }

        sql_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        for sql_file in sql_files:
            if sql_file.name in applied:
                logger.debug("[db] Already applied: %s", sql_file.name)
                continue

            sql = sql_file.read_text()
            logger.info("[db] Applying migration: %s", sql_file.name)
            await con.execute(sql)
            await con.execute(
                "INSERT INTO _migrations (name) VALUES ($1)", sql_file.name
            )
            logger.info("[db] Migration applied: %s", sql_file.name)
