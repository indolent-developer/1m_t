"""
core.utils.market

Market session time helpers (US Eastern Time).
"""
from __future__ import annotations

import datetime as dt

import pytz

_ET = pytz.timezone("America/New_York")

# Regular session: 09:30 – 16:00 ET
_OPEN  = dt.time(9,  30)
_CLOSE = dt.time(16,  0)

# Extended hours: pre-market 04:00–09:30, post-market 16:00–20:00
_PRE_OPEN    = dt.time(4,  0)
_POST_CLOSE  = dt.time(20, 0)


def now_et() -> dt.datetime:
    return dt.datetime.now(_ET)


def is_regular_market_time() -> bool:
    t = now_et().time()
    return _OPEN <= t < _CLOSE


def is_extended_market_time() -> bool:
    """Return True during pre-market (04:00–09:30) or post-market (16:00–20:00) ET."""
    t = now_et().time()
    return (_PRE_OPEN <= t < _OPEN) or (_CLOSE <= t < _POST_CLOSE)


def is_pre_market_time() -> bool:
    t = now_et().time()
    return _PRE_OPEN <= t < _OPEN


def is_post_market_time() -> bool:
    t = now_et().time()
    return _CLOSE <= t < _POST_CLOSE
