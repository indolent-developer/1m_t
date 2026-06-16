"""Persist filled/rejected orders into deal.orders + deal.trades."""
from __future__ import annotations

from core.utils.log_helper import getLogger
from datetime import timezone

from core.entities.broker_entities import Order, OrderStatus
from infrastructure.repositories.base import BaseRepository

logger = getLogger(__name__)


class TradeRepository(BaseRepository):

    async def save_order(self, order: Order, account_id: int) -> int | None:
        """Insert into deal.orders. Returns the new order_id."""
        try:
            side   = order.side.value.upper() if hasattr(order.side, "value") else str(order.side).upper()
            status = order.status.value.upper() if hasattr(order.status, "value") else str(order.status).upper()
            row = await self._fetchrow(
                """
                INSERT INTO deal.orders
                    (external_order_id, account_id, type, side, quantity,
                     limit_price, status, submitted_at, raw_request)
                VALUES ($1, $2, 'MARKET', $3, $4, $5, $6, $7, '{}')
                ON CONFLICT (external_order_id) DO NOTHING
                RETURNING order_id
                """,
                order.broker_order_id,
                account_id,
                side,
                order.quantity,
                order.price or None,
                status,
                order.placed_timestamp.replace(tzinfo=timezone.utc)
                    if order.placed_timestamp and order.placed_timestamp.tzinfo is None
                    else order.placed_timestamp,
            )
            return row["order_id"] if row else None
        except Exception as e:
            logger.warning("[TradeRepository] save_order failed: %s", e)
            return None

    async def save_fill(self, order: Order, db_order_id: int, deal_id: int) -> None:
        """Insert a fill row into deal.trades for a FILLED order."""
        try:
            if order.status != OrderStatus.FILLED:
                return
            side = order.side.value.upper() if hasattr(order.side, "value") else str(order.side).upper()
            fill_ts = order.filled_timestamp or order.placed_timestamp
            await self._execute(
                """
                INSERT INTO deal.trades
                    (external_trade_id, order_id, deal_id, side, quantity,
                     price, commission, trade_time)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (external_trade_id) DO NOTHING
                """,
                order.broker_order_id,
                db_order_id,
                deal_id,
                side,
                order.quantity,
                order.average_fill_price or order.price or 0,
                order.fees or 0,
                fill_ts,
            )
        except Exception as e:
            logger.warning("[TradeRepository] save_fill failed: %s", e)

    async def list_recent(self, account_id: int, limit: int = 50) -> list:
        try:
            return await self._fetch(
                """
                SELECT o.external_order_id, o.side, o.quantity, o.status,
                       o.submitted_at, t.price, t.commission
                FROM deal.orders o
                LEFT JOIN deal.trades t ON t.order_id = o.order_id
                WHERE o.account_id = $1
                ORDER BY o.submitted_at DESC
                LIMIT $2
                """,
                account_id, limit,
            )
        except Exception as e:
            logger.warning("[TradeRepository] list_recent failed: %s", e)
            return []
