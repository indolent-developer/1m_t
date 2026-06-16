"""Tests for OHLCData, Quote, DataSource, MarketStatus, GradeType."""
import datetime as dt
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from core.entities.ohlc import OHLCData
from core.entities.market_quotes import Quote
from core.entities.instrument_type import InstrumentType
from core.entities.broker_entities import DataSource
from core.entities.market_data import MarketSession, MarketStatus
from core.entities.analyst_data import GradeType


# ── OHLCData ──────────────────────────────────────────────────────────────────

def test_ohlc_p_change_up():
    bar = OHLCData(open=100, high=110, low=95, close=110)
    assert bar.p_change == pytest.approx(10.0)


def test_ohlc_p_change_down():
    bar = OHLCData(open=100, high=100, low=90, close=90)
    assert bar.p_change == pytest.approx(-10.0)


def test_ohlc_p_change_zero_open():
    bar = OHLCData(open=0, high=10, low=0, close=5)
    assert bar.p_change == 0.0


def test_ohlc_timestamp_derived():
    t = dt.datetime(2025, 6, 1, 9, 30, 0)
    bar = OHLCData(open=100, high=110, low=95, close=105, time=t)
    assert bar.t_str == "2025-06-01 09:30:00"
    assert bar.timestamp == int(t.timestamp())


def test_ohlc_no_time_fields_none():
    bar = OHLCData(open=100, high=110, low=95, close=105)
    assert bar.t_str is None
    assert bar.timestamp is None


def test_ohlc_shorthand_aliases():
    bar = OHLCData(open=1, high=2, low=3, close=4, volume=500)
    assert bar.o == 1 and bar.h == 2 and bar.l == 3 and bar.c == 4 and bar.v == 500


def test_ohlc_from_dict():
    bar = OHLCData.from_dict({"o": "10", "h": "12", "l": "9", "c": "11", "v": "1000"})
    assert bar.open == 10.0 and bar.close == 11.0 and bar.volume == 1000.0


def test_ohlc_to_dict_roundtrip():
    bar = OHLCData(open=10, high=12, low=9, close=11, volume=500)
    d = bar.to_dict()
    assert d["o"] == 10 and d["c"] == 11 and d["v"] == 500


# ── Quote ─────────────────────────────────────────────────────────────────────

def _quote(bid="99.50", ask="100.50"):
    return Quote(
        symbol="AAPL",
        instrument_type=InstrumentType.STOCK,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal("100.00"),
        bid_size=100,
        ask_size=100,
        volume=1_000_000,
        timestamp=dt.datetime(2025, 1, 1, 10, 0),
    )


def test_quote_mid():
    q = _quote("99.00", "101.00")
    assert q.mid == Decimal("100.00")


def test_quote_spread():
    q = _quote("99.00", "101.00")
    assert q.spread == Decimal("2.00")


def test_quote_from_dict():
    ts = 1_735_725_600_000  # ms timestamp
    q = Quote.from_dict({
        "symbol": "MSFT",
        "instrument_type": "stock",
        "bidPrice": "415.00",
        "askPrice": "415.10",
        "lastPrice": "415.05",
        "bidSize": 10,
        "askSize": 20,
        "volume": 5000,
        "timestamp": ts,
    })
    assert q.symbol == "MSFT"
    assert q.bid == Decimal("415.00")


# ── DataSource ────────────────────────────────────────────────────────────────

def test_datasource_parse_valid():
    assert DataSource.parse("capital") == DataSource.CAPITAL
    assert DataSource.parse("fmp") == DataSource.FMP


def test_datasource_parse_invalid():
    with pytest.raises(ValueError, match="Invalid data_source"):
        DataSource.parse("unknown")


# ── MarketStatus ──────────────────────────────────────────────────────────────

def test_market_status_from_dict_open():
    ms = MarketStatus.from_dict({
        "exchange": "NYSE",
        "isOpen": True,
        "session": "regular",
        "timezone": "America/New_York",
        "t": 1_735_725_600,
    })
    assert ms.is_open is True
    assert ms.session == MarketSession.REGULAR
    assert ms.holiday is None


def test_market_status_from_dict_holiday():
    ms = MarketStatus.from_dict({
        "exchange": "NYSE",
        "isOpen": False,
        "session": "closed",
        "timezone": "America/New_York",
        "t": 0,
        "holiday": "Independence Day",
    })
    assert ms.is_open is False
    assert ms.session == MarketSession.CLOSED
    assert ms.holiday == "Independence Day"


# ── GradeType ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("Buy",          GradeType.BUY),
    ("Strong Buy",   GradeType.STRONG_BUY),
    ("Hold",         GradeType.HOLD),
    ("Sell",         GradeType.SELL),
    ("Outperform",   GradeType.OUTPERFORM),
    ("Neutral",      GradeType.NEUTRAL),
    ("Underweight",  GradeType.UNDERWEIGHT),
    ("Overweight",   GradeType.OVERWEIGHT),
])
def test_grade_type_roundtrip(value, expected):
    assert GradeType(value) == expected


def test_grade_type_invalid():
    with pytest.raises(ValueError):
        GradeType("NotARating")
