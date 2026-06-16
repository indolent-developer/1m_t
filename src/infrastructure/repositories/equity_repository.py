"""Log account equity snapshots for the equity curve."""
from __future__ import annotations

from core.utils.log_helper import getLogger
from datetime import datetime, timezone

from core.entities.broker_entities import AccountInfo
from infrastructure.repositories.base import BaseRepository

logger = getLogger(__name__)


class EquityRepository(BaseRepository):

    async def log(self, account: AccountInfo, account_id: int, broker_id: str) -> None:
        try:
            await self._execute(
                """
                INSERT INTO equity_log
                    (account_id, broker_id, total_value, cash, unrealized_pnl, logged_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                account_id,
                broker_id,
                account.current_value or 0,
                account.cash_in_hand  or 0,
                None,
                datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning("[EquityRepository] log failed: %s", e)

    async def history(self, account_id: int, limit: int = 90) -> list:
        try:
            return await self._fetch(
                """
                SELECT total_value, cash, unrealized_pnl, logged_at
                FROM equity_log
                WHERE account_id = $1
                ORDER BY logged_at DESC
                LIMIT $2
                """,
                account_id, limit,
            )
        except Exception as e:
            logger.warning("[EquityRepository] history failed: %s", e)
            return []
