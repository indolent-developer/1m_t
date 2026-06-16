"""
core.entities.time_frame

Standard OHLC/data timeframe enum.
"""
from enum import Enum


class TimeFrame(Enum):
    MINUTE_1  = "1minute"
    MINUTE_5  = "5minutes"
    MINUTE_15 = "15minutes"
    MINUTE_30 = "30minutes"
    HOUR_1    = "1hour"
    HOUR_4    = "4hours"
    DAY       = "1day"
    WEEK      = "1week"
    MONTH     = "1month"
