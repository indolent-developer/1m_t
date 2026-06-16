"""Tests for Deal, OrderLeg, TpLevel, DealState accounting logic."""
import datetime as dt
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from core.entities.deal import Deal, DealState, OrderLeg, TpLevel
from core.entities.broker_entities import TradeSide
from core.entities.position_types import Position

_NOW = dt.datetime(2025, 1, 1, 10, 0, 0)


def _leg(order_id, qty, fill_price, commission=0.0, trigger="SIGNAL"):
    return OrderLeg(
        order_id=order_id,
        qty=qty,
        fill_price=fill_price,
        filled_at=_NOW,
        commission=commission,
        trigger=trigger,
    )


def _deal(direction=TradeSide.LONG.value):
    return Deal(deal_id="D1", symbol="AAPL", direction=direction)


# ── DealState transitions ──────────────────────────────────────────────────────

def test_new_deal_is_pending():
    assert _deal().state == DealState.PENDING


def test_add_entry_moves_to_open():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    assert d.state == DealState.OPEN


def test_full_exit_closes_deal():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.add_exit(_leg("O2", qty=10, fill_price=110))
    assert d.state == DealState.CLOSED
    assert d.remaining_qty == 0.0


def test_partial_exit_stays_open():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.add_exit(_leg("O2", qty=4, fill_price=110))
    assert d.state == DealState.OPEN
    assert d.remaining_qty == pytest.approx(6.0)


# ── avg_entry_price (averaging-in) ────────────────────────────────────────────

def test_single_entry_avg_price():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    assert d.avg_entry_price == pytest.approx(100.0)
    assert d.total_qty == pytest.approx(10.0)


def test_averaging_in_two_legs():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.add_entry(_leg("O2", qty=10, fill_price=120))
    # (10*100 + 10*120) / 20 = 110
    assert d.avg_entry_price == pytest.approx(110.0)
    assert d.total_qty == pytest.approx(20.0)


def test_averaging_in_unequal_sizes():
    d = _deal()
    d.add_entry(_leg("O1", qty=5,  fill_price=100))
    d.add_entry(_leg("O2", qty=15, fill_price=120))
    # (5*100 + 15*120) / 20 = (500 + 1800) / 20 = 115
    assert d.avg_entry_price == pytest.approx(115.0)


# ── realized_pnl ──────────────────────────────────────────────────────────────

def test_long_profit():
    d = _deal(TradeSide.LONG.value)
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    gross, _ = d.add_exit(_leg("O2", qty=10, fill_price=110))
    assert gross == pytest.approx(100.0)          # (110-100)*10
    assert d.realized_pnl == pytest.approx(100.0)


def test_long_loss():
    d = _deal(TradeSide.LONG.value)
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    gross, _ = d.add_exit(_leg("O2", qty=10, fill_price=90))
    assert gross == pytest.approx(-100.0)
    assert d.realized_pnl == pytest.approx(-100.0)


def test_short_profit():
    d = _deal(TradeSide.SHORT.value)
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    gross, _ = d.add_exit(_leg("O2", qty=10, fill_price=90))
    # short profits when price drops: (90-100)*10 * -1 = +100
    assert gross == pytest.approx(100.0)
    assert d.realized_pnl == pytest.approx(100.0)


def test_short_loss():
    d = _deal(TradeSide.SHORT.value)
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    gross, _ = d.add_exit(_leg("O2", qty=10, fill_price=110))
    assert gross == pytest.approx(-100.0)


# ── Commission accounting ──────────────────────────────────────────────────────

def test_commission_reduces_pnl():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100, commission=5.0))
    d.add_exit(_leg("O2", qty=10, fill_price=110, commission=5.0))
    # gross=100, entry_comm=5, exit_comm=5 → net=90
    assert d.realized_pnl == pytest.approx(90.0)


def test_partial_exit_proportional_commission():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100, commission=10.0))
    # Exit half → should allocate half the entry commission
    gross, entry_comm = d.add_exit(_leg("O2", qty=5, fill_price=110, commission=0.0))
    assert entry_comm == pytest.approx(5.0)
    # Remaining entry commission should be 5.0
    assert d._remaining_entry_commission == pytest.approx(5.0)


def test_two_partial_exits_commission_fully_consumed():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100, commission=10.0))
    d.add_exit(_leg("O2", qty=5, fill_price=110, commission=0.0))
    d.add_exit(_leg("O3", qty=5, fill_price=110, commission=0.0))
    assert d._remaining_entry_commission == pytest.approx(0.0)
    assert d.state == DealState.CLOSED


# ── avg_exit_price ────────────────────────────────────────────────────────────

def test_avg_exit_price_single():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.add_exit(_leg("O2", qty=10, fill_price=115))
    assert d.avg_exit_price == pytest.approx(115.0)


def test_avg_exit_price_two_partials():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.add_exit(_leg("O2", qty=4, fill_price=110))
    d.add_exit(_leg("O3", qty=6, fill_price=120))
    # (4*110 + 6*120) / 10 = (440 + 720) / 10 = 116
    assert d.avg_exit_price == pytest.approx(116.0)


# ── Convenience properties ────────────────────────────────────────────────────

def test_entry_date():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    assert d.entry_date == _NOW


def test_entry_date_none_before_entry():
    assert _deal().entry_date is None


def test_entry_bar_index_default():
    d = _deal()
    d.add_entry(OrderLeg("O1", 10, 100, _NOW, 0, "SIGNAL", bar_index=5))
    assert d.entry_bar_index == 5


# ── Position integration ──────────────────────────────────────────────────────

def _position(unrealized_pnl=250.0, market_value=1050.0):
    return Position(
        id="P1", symbol="AAPL", side=TradeSide.LONG,
        open_date=_NOW, close_date=None,
        quantity=10, average_price=100.0, leverage=1.0,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl, unrealized_pnl_percentage=2.5,
        realized_pnl=0.0, realized_pnl_percentage=0.0,
        stop_loss_price=95.0, take_profit_price=115.0,
    )


def test_unrealized_pnl_no_positions():
    assert _deal().unrealized_pnl == 0.0


def test_market_value_no_positions():
    assert _deal().market_value == 0.0


def test_unrealized_pnl_single_position():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.positions = [_position(unrealized_pnl=250.0)]
    assert d.unrealized_pnl == pytest.approx(250.0)


def test_market_value_single_position():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.positions = [_position(market_value=1050.0)]
    assert d.market_value == pytest.approx(1050.0)


def test_unrealized_pnl_multiple_positions():
    # Capital.com style: two open positions on same symbol (e.g. scaled-in)
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.positions = [
        _position(unrealized_pnl=150.0, market_value=600.0),
        _position(unrealized_pnl=100.0, market_value=400.0),
    ]
    assert d.unrealized_pnl == pytest.approx(250.0)
    assert d.market_value   == pytest.approx(1000.0)


def test_total_pnl_combines_realized_and_unrealized():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.add_exit(_leg("O2", qty=5, fill_price=110))    # realized = +50
    d.positions = [_position(unrealized_pnl=30.0)]   # unrealized on remaining 5
    assert d.realized_pnl == pytest.approx(50.0)
    assert d.total_pnl    == pytest.approx(80.0)


def test_positions_cleared_on_full_close():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.positions = [_position()]
    d.add_exit(_leg("O2", qty=10, fill_price=110))
    assert d.state         == DealState.CLOSED
    assert d.positions     == []
    assert d.unrealized_pnl == 0.0


def test_positions_retained_on_partial_close():
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    d.positions = [_position(unrealized_pnl=100.0)]
    d.add_exit(_leg("O2", qty=4, fill_price=110))
    assert d.state     == DealState.OPEN
    assert d.positions != []


def test_opposite_side_positions():
    # Capital.com allows long and short on same symbol simultaneously
    d = _deal()
    d.add_entry(_leg("O1", qty=10, fill_price=100))
    long_pos  = _position(unrealized_pnl= 200.0, market_value=1100.0)
    short_pos = _position(unrealized_pnl=-50.0,  market_value= 450.0)
    d.positions = [long_pos, short_pos]
    assert d.unrealized_pnl == pytest.approx(150.0)
    assert d.market_value   == pytest.approx(1550.0)
