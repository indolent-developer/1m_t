from enum import Enum


class BrokerEvent(str, Enum):
    """All events a broker adapter can emit."""

    # Connection lifecycle
    CONNECTED          = "connected"
    DISCONNECTED       = "disconnected"
    CONNECTION_LOST    = "connection_lost"    # unexpected drop → triggers reconnect
    RECONNECTING       = "reconnecting"

    # Order lifecycle
    ORDER_SUBMITTED    = "order_submitted"    # ACK from broker, not yet filled
    ORDER_FILLED       = "order_filled"
    ORDER_PARTIAL_FILL = "order_partial_fill"
    ORDER_REJECTED     = "order_rejected"
    ORDER_CANCELLED    = "order_cancelled"
    ORDER_EXPIRED      = "order_expired"

    # Position lifecycle
    POSITION_OPENED    = "position_opened"
    POSITION_UPDATED   = "position_updated"  # stop/TP change, partial close
    POSITION_CLOSED    = "position_closed"

    # Market data (forwarded from data service via broker)
    QUOTE_UPDATE       = "quote_update"       # streaming tick
    ACCOUNT_UPDATE     = "account_update"     # equity / buying power changed

    # Risk guardrails (fired by check_risk_limits)
    DAILY_LOSS_LIMIT   = "daily_loss_limit"   # hard max loss hit → go to cash
    EQUITY_FLOOR_HIT   = "equity_floor_hit"   # own equity < $55k loan guardrail

