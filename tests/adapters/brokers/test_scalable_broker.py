"""
Tests for ScalableBroker.

All CLI subprocess calls are mocked — no real sc binary needed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from adapters.brokers.scalable_broker import ScalableBroker
from adapters.brokers.entities.broker_event import BrokerEvent
from core.config.config_models import ScalableBrokerConfig
from core.entities.broker_entities import OrderSide, OrderStatus, OrderType


# ── Helpers ───────────────────────────────────────────────────────────────────

def _config(**kwargs) -> ScalableBrokerConfig:
    defaults = dict(cli_path="sc", readonly=False,
                    poll_interval_seconds=0.01, poll_timeout_seconds=1.0,
                    poll_max_attempts=5)
    defaults.update(kwargs)
    return ScalableBrokerConfig(**defaults)


def _broker(**kwargs) -> ScalableBroker:
    return ScalableBroker(_config(**kwargs))


def _cli_response(data: dict | list) -> MagicMock:
    """Mock asyncio.create_subprocess_exec to return JSON data."""
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(json.dumps(data).encode(), b""))
    return proc


def _cli_error() -> MagicMock:
    """Mock a failing CLI call."""
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"error"))
    return proc


def _phase1_response(confirm_id: str = "CONF-1") -> MagicMock:
    """Realistic sc phase-1 trade response envelope."""
    return _cli_response({
        "data": {
            "confirmation": {"id": confirm_id, "expires_at_epoch": 9999999999},
            "result": {
                "intent":       {"isin": "US0378331005", "amount": "1850.00", "order_type": "market"},
                "calculation":  {"shares": "10", "estimated_order_volume": "1850.00"},
                "market_quote": {"ask_price": "186.00", "bid_price": "184.00",
                                 "mid_price": "185.00", "currency": "EUR"},
                "tradability":  {"selected_venue_label": "XNAS"},
                "ex_ante_costs": {"entryCosts": {"total": {"amount": "0.99"}}},
            },
        }
    })


def _phase2_response(order_id: str = "ORD-123") -> MagicMock:
    """Realistic sc phase-2 trade response envelope."""
    return _cli_response({
        "data": {
            "result": {
                "order_submission": {"order_id": order_id}
            }
        }
    })


def _holdings_response(items: list | None = None) -> MagicMock:
    """Mock broker holdings."""
    return _cli_response({"count": len(items or []), "items": items or []})


def _quote_response(ask: float = 186.0, bid: float = 184.0, mid: float = 185.0) -> MagicMock:
    return _cli_response({
        "quote_ask_price": ask,
        "quote_bid_price": bid,
        "quote_mid_price": mid,
    })


AAPL_HOLDING = {
    "name": "Apple",
    "isin": "US0378331005",
    "quantity": 10.0,
    "fifo_price": 180.0,
    "quote_mid_price": 185.0,
    "valuation": 1850.0,
}


# ── _run_cli ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_cli_returns_parsed_json():
    broker = _broker()
    payload = {"accountId": "123", "cash": 5000.0}
    with patch("asyncio.create_subprocess_exec", return_value=_cli_response(payload)):
        result = await broker._run_cli(["account", "summary"])
    assert result == payload


@pytest.mark.asyncio
async def test_run_cli_returns_none_on_error():
    broker = _broker()
    with patch("asyncio.create_subprocess_exec", return_value=_cli_error()):
        result = await broker._run_cli(["account", "summary"])
    assert result is None


@pytest.mark.asyncio
async def test_run_cli_raises_on_file_not_found():
    broker = _broker()
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="sc binary not found"):
            await broker._run_cli(["account", "summary"])


@pytest.mark.asyncio
async def test_run_cli_raises_on_invalid_json():
    broker = _broker()
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"not-json{{{", b""))
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(RuntimeError, match="non-JSON"):
            await broker._run_cli(["account", "summary"])


@pytest.mark.asyncio
async def test_run_cli_appends_json_flag():
    broker = _broker(cli_path="sc")
    captured = []

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return _cli_response({})

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await broker._run_cli(["orders", "list"])

    assert "--json" in captured


# ── connect ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_success_emits_connected():
    broker = _broker()
    events = []
    broker.events.subscribe(BrokerEvent.CONNECTED, lambda p: events.append(p.event))

    with patch("asyncio.create_subprocess_exec", return_value=_cli_response({"accountId": "1"})):
        result = await broker.connect()

    assert result is True
    assert broker._connected is True
    assert BrokerEvent.CONNECTED in events


@pytest.mark.asyncio
async def test_connect_failure_emits_connection_lost():
    broker = _broker()
    events = []
    broker.events.subscribe(BrokerEvent.CONNECTION_LOST, lambda p: events.append(p.event))

    with patch("asyncio.create_subprocess_exec", return_value=_cli_error()):
        result = await broker.connect()

    assert result is False
    assert broker._connected is False
    assert BrokerEvent.CONNECTION_LOST in events


# ── _require_connected guard ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_account_raises_if_not_connected():
    broker = _broker()
    with pytest.raises(RuntimeError, match="Not connected"):
        await broker.get_account_info()


@pytest.mark.asyncio
async def test_get_positions_raises_if_not_connected():
    broker = _broker()
    with pytest.raises(RuntimeError, match="Not connected"):
        await broker.get_positions()


# ── readonly guard ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_order_blocked_in_readonly():
    broker = _broker(readonly=True)
    broker._connected = True
    with pytest.raises(RuntimeError, match="readonly=True"):
        await broker.place_order("AAPL", 10, OrderSide.BUY, OrderType.MARKET)


# ── get_account_info ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_account_info_maps_fields():
    broker = _broker()
    broker._connected = True
    payload = {
        "account_id": "ACC1",
        "valuation": {"total": 75000.0, "securities": 65000.0},
    }

    with patch("asyncio.create_subprocess_exec", return_value=_cli_response(payload)):
        info = await broker.get_account_info()

    assert info.account_id    == "ACC1"
    assert info.account_name  == "Scalable Capital"
    assert info.current_value == pytest.approx(75000.0)
    assert info.cash_in_hand  == pytest.approx(10000.0)
    assert info.currency      == "EUR"


@pytest.mark.asyncio
async def test_get_account_info_cached():
    broker = _broker()
    broker._connected = True
    payload = {"accountId": "1", "totalValue": 1000.0, "cash": 500.0}

    call_count = 0
    async def fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _cli_response(payload)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await broker.get_account_info()
        await broker.get_account_info()

    assert call_count == 1


@pytest.mark.asyncio
async def test_get_account_info_emits_account_update():
    broker = _broker()
    broker._connected = True
    events = []
    broker.events.subscribe(BrokerEvent.ACCOUNT_UPDATE, lambda p: events.append(p.event))

    with patch("asyncio.create_subprocess_exec",
               return_value=_cli_response({"totalValue": 1000.0, "cash": 500.0})):
        await broker.get_account_info()

    assert BrokerEvent.ACCOUNT_UPDATE in events


# ── get_positions ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_positions_maps_list():
    broker = _broker()
    broker._connected = True
    payload = {"count": 2, "items": [
        {"name": "AAPL", "isin": "US0378331005",
         "quantity": 10.0, "fifo_price": 180.0, "quote_mid_price": 185.0, "valuation": 1850.0},
        {"name": "MSFT", "isin": "US5949181045",
         "quantity": 5.0,  "fifo_price": 400.0, "quote_mid_price": 410.0, "valuation": 2050.0},
    ]}
    with patch("asyncio.create_subprocess_exec", return_value=_cli_response(payload)):
        positions = await broker.get_positions()

    assert len(positions) == 2
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == 10.0
    assert positions[1].symbol == "MSFT"


@pytest.mark.asyncio
async def test_get_positions_filter_by_symbol():
    broker = _broker()
    broker._connected = True
    payload = {"count": 2, "items": [
        {"name": "AAPL", "isin": "US0378331005",
         "quantity": 10.0, "fifo_price": 180.0, "quote_mid_price": 185.0, "valuation": 1850.0},
        {"name": "MSFT", "isin": "US5949181045",
         "quantity": 5.0,  "fifo_price": 400.0, "quote_mid_price": 410.0, "valuation": 2050.0},
    ]}
    with patch("asyncio.create_subprocess_exec", return_value=_cli_response(payload)):
        positions = await broker.get_positions("AAPL")

    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_get_position_returns_none_if_missing():
    broker = _broker()
    broker._connected = True
    with patch("asyncio.create_subprocess_exec", return_value=_holdings_response([])):
        pos = await broker.get_position("NVDA")
    assert pos is None


# ── place_order ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_order_returns_filled():
    broker = _broker()
    broker._connected = True
    broker._isin_cache["AAPL"] = "US0378331005"
    broker._load_ticker_overrides = lambda: {}

    responses = [
        _quote_response(),      # broker quote
        _phase1_response(),     # trade phase-1
        _phase2_response(),     # trade phase-2 → ORDER_SUBMITTED emitted
        # poll: transactions list contains the fill
        _cli_response({"items": [{
            "isin": "US0378331005", "status": "SETTLED",
            "side": "BUY", "quantity": 10.0, "amount": 1860.0,
        }]}),
    ]
    with patch("asyncio.create_subprocess_exec", side_effect=responses):
        order = await broker.place_order("AAPL", 10, OrderSide.BUY, OrderType.MARKET)

    assert order.status == OrderStatus.FILLED
    assert order.symbol == "AAPL"
    assert order.broker_order_id == "ORD-123"


@pytest.mark.asyncio
async def test_place_order_emits_order_submitted():
    broker = _broker()
    broker._connected = True
    broker._isin_cache["AAPL"] = "US0378331005"
    broker._load_ticker_overrides = lambda: {}
    events = []
    broker.events.subscribe(BrokerEvent.ORDER_SUBMITTED, lambda p: events.append(p.event))

    responses = [
        _quote_response(), _phase1_response(), _phase2_response(),
        _cli_response({"items": [{
            "isin": "US0378331005", "status": "SETTLED",
            "side": "BUY", "quantity": 5.0, "amount": 930.0,
        }]}),
    ]
    with patch("asyncio.create_subprocess_exec", side_effect=responses):
        await broker.place_order("AAPL", 5, OrderSide.BUY, OrderType.MARKET)

    assert BrokerEvent.ORDER_SUBMITTED in events


@pytest.mark.asyncio
async def test_place_sell_without_position_raises():
    broker = _broker()
    broker._connected = True
    broker._isin_cache["AAPL"] = "US0378331005"

    responses = [
        _holdings_response([]),  # get_positions() — no positions
    ]
    with patch("asyncio.create_subprocess_exec", side_effect=responses):
        with pytest.raises(RuntimeError, match="long-only"):
            await broker.place_order("AAPL", 5, OrderSide.SELL, OrderType.MARKET)


# ── preview_order ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preview_order_buy_returns_preview_and_confirm_id():
    broker = _broker()
    broker._connected = True
    broker._isin_cache["AAPL"] = "US0378331005"

    responses = [_quote_response(), _phase1_response("CONF-BUY")]
    with patch("asyncio.create_subprocess_exec", side_effect=responses):
        preview, confirm_id = await broker.preview_order(
            "AAPL", 10, OrderSide.BUY, OrderType.MARKET
        )

    assert confirm_id == "CONF-BUY"
    assert preview["isin"] == "US0378331005"
    assert preview["_isin"] == "US0378331005"
    assert preview["_action"] == "buy"


@pytest.mark.asyncio
async def test_preview_order_sell_succeeds_when_position_exists():
    """SELL guard checks position by ISIN (not company name)."""
    broker = _broker()
    broker._connected = True
    broker._isin_cache["NFLX"] = "US64110W1027"
    broker._load_ticker_overrides = lambda: {}  # prevent ticker_isin.json from overriding test ISIN

    responses = [
        # get_positions() call from the SELL guard
        _holdings_response([{
            "name": "Netflix", "isin": "US64110W1027",
            "quantity": 50.0, "fifo_price": 600.0,
            "quote_mid_price": 700.0, "valuation": 35000.0,
        }]),
        _quote_response(ask=710.0, bid=690.0, mid=700.0),
        _phase1_response("CONF-SELL"),
    ]
    with patch("asyncio.create_subprocess_exec", side_effect=responses):
        preview, confirm_id = await broker.preview_order(
            "NFLX", 10, OrderSide.SELL, OrderType.MARKET
        )

    assert confirm_id == "CONF-SELL"
    assert preview["_action"] == "sell"


@pytest.mark.asyncio
async def test_preview_order_sell_raises_when_no_position():
    broker = _broker()
    broker._connected = True
    broker._isin_cache["ZZZZ"] = "US9999999999"

    responses = [
        _holdings_response([]),   # no positions
    ]
    with patch("asyncio.create_subprocess_exec", side_effect=responses):
        with pytest.raises(RuntimeError, match="no existing position"):
            await broker.preview_order("ZZZZ", 5, OrderSide.SELL, OrderType.MARKET)


@pytest.mark.asyncio
async def test_preview_order_stop_buy_includes_stop_price_in_args():
    """Stop buy order should pass --order-type stop --stop-price to sc CLI."""
    broker = _broker()
    broker._connected = True
    broker._isin_cache["PLTR"] = "US69608A1088"

    captured_args = []
    original_run_cli = broker._run_cli

    call_count = 0
    async def capturing_run_cli(args, **kwargs):
        nonlocal call_count
        call_count += 1
        captured_args.append(list(args))
        if call_count == 1:
            return {"quote_ask_price": 108.0, "quote_bid_price": 106.0, "quote_mid_price": 107.0}
        return {
            "data": {
                "confirmation": {"id": "CONF-STOP"},
                "result": {
                    "intent": {"isin": "US69608A1088", "amount": "", "order_type": "stop"},
                    "calculation": {"shares": "27"},
                    "market_quote": {"ask_price": "108.0", "bid_price": "106.0",
                                     "mid_price": "107.0", "currency": "EUR"},
                    "tradability": {"selected_venue_label": "XNAS"},
                    "ex_ante_costs": {},
                }
            }
        }

    broker._run_cli = capturing_run_cli

    preview, confirm_id = await broker.preview_order(
        "PLTR", 27, OrderSide.BUY, OrderType.STOP, price=107.80
    )

    assert confirm_id == "CONF-STOP"
    trade_args = next(a for a in captured_args if "trade" in a)
    assert "--order-type" in trade_args
    assert "stop" in trade_args
    assert "--stop-price" in trade_args
    stop_idx = trade_args.index("--stop-price")
    assert float(trade_args[stop_idx + 1]) == pytest.approx(107.80, abs=0.001)


# ── submit_order ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_order_extracts_order_id_from_envelope():
    broker = _broker()
    broker._connected = True

    async def _skip_poll(order_id, submitted_order, *, isin=""):
        return submitted_order
    broker._poll_order_status_sync = _skip_poll

    preview = {
        "_base_args": ["broker", "trade", "buy", "--isin", "US0378331005", "--amount", "1850.0"],
        "_isin": "US0378331005",
        "_action": "buy",
    }
    with patch("asyncio.create_subprocess_exec", return_value=_phase2_response("ORD-456")):
        order = await broker.submit_order(
            "AAPL", 10, OrderSide.BUY, "CONF-1", preview, OrderType.MARKET
        )

    assert order.broker_order_id == "ORD-456"
    assert order.status == OrderStatus.SUBMITTED
    assert order.symbol == "AAPL"


@pytest.mark.asyncio
async def test_submit_order_raises_on_missing_order_id():
    broker = _broker()
    broker._connected = True

    preview = {"_base_args": ["broker", "trade", "buy", "--isin", "US123", "--amount", "100"]}
    with patch("asyncio.create_subprocess_exec",
               return_value=_cli_response({"data": {"result": {}}})):
        with pytest.raises(RuntimeError, match="No order id"):
            await broker.submit_order(
                "AAPL", 5, OrderSide.BUY, "CONF-X", preview, OrderType.MARKET
            )


# ── stop order end-to-end via place_order ─────────────────────────────────────

@pytest.mark.asyncio
async def test_place_stop_sell_order():
    broker = _broker()
    broker._connected = True
    broker._isin_cache["WOLF"] = "DE000A2H9AX3"
    broker._load_ticker_overrides = lambda: {}  # prevent ticker_isin.json from overriding test ISIN

    responses = [
        # SELL guard: get_positions() — position exists
        _holdings_response([{
            "name": "WOLF", "isin": "DE000A2H9AX3",
            "quantity": 100.0, "fifo_price": 5.0,
            "quote_mid_price": 4.0, "valuation": 400.0,
        }]),
        _quote_response(ask=4.1, bid=3.9, mid=4.0),
        _phase1_response("CONF-STOP-SELL"),
        _phase2_response("ORD-STOP-789"),
    ]
    with patch("asyncio.create_subprocess_exec", side_effect=responses):
        order = await broker.place_order(
            "WOLF", 50, OrderSide.SELL, OrderType.STOP, price=3.50
        )

    # Cancel the background poll task that STOP orders launch
    for t in list(broker._order_poll_tasks.values()):
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    assert order.status == OrderStatus.SUBMITTED
    assert order.broker_order_id == "ORD-STOP-789"


# ── _parse_phase1 ──────────────────────────────────────────────────────────────

def test_parse_phase1_extracts_fields():
    from adapters.brokers.scalable_broker import _parse_phase1

    p1d = {
        "confirmation": {"id": "CONF-99", "expires_at_epoch": 9999},
        "result": {
            "intent":       {"isin": "US0378331005", "amount": "1850.00", "order_type": "market"},
            "calculation":  {"shares": "10", "estimated_order_volume": "1850.00"},
            "market_quote": {"ask_price": "186.00", "bid_price": "184.00",
                             "mid_price": "185.00", "currency": "EUR"},
            "tradability":  {"selected_venue_label": "XNAS"},
            "ex_ante_costs": {"entryCosts": {"total": {"amount": "0.99"}}},
        },
    }
    result = _parse_phase1(p1d)

    assert result["isin"]      == "US0378331005"
    assert result["ask"]       == "186.00"
    assert result["bid"]       == "184.00"
    assert result["currency"]  == "EUR"
    assert result["confirm_id"] == "CONF-99"
    assert result["fee_entry"] == "0.99"
    assert result["venue"]     == "XNAS"


# ── _poll_order_status ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_order_filled_emits_order_filled():
    broker = _broker()
    broker._connected = True
    events = []
    broker.events.subscribe(BrokerEvent.ORDER_FILLED, lambda p: events.append(p))

    submitted = MagicMock()
    submitted.symbol           = "AAPL"
    submitted.order_type       = OrderType.MARKET
    submitted.side             = OrderSide.BUY
    submitted.quantity         = 10.0
    submitted.price            = 0.0
    submitted.placed_timestamp = None

    fill_data = {"status": "SETTLED", "quantity": 10.0, "amount": 1860.0, "isin": "US0378"}

    with patch("asyncio.create_subprocess_exec", return_value=_cli_response(fill_data)):
        await broker._poll_order_status("ORD1", submitted)

    assert len(events) == 1
    assert events[0].data.status             == OrderStatus.FILLED
    assert events[0].data.average_fill_price == pytest.approx(186.0)


@pytest.mark.asyncio
async def test_poll_order_rejected_emits_order_rejected():
    broker = _broker()
    broker._connected = True
    events = []
    broker.events.subscribe(BrokerEvent.ORDER_REJECTED, lambda p: events.append(p))

    submitted = MagicMock()
    submitted.symbol           = "AAPL"
    submitted.order_type       = OrderType.MARKET
    submitted.side             = OrderSide.BUY
    submitted.quantity         = 10.0
    submitted.price            = 0.0
    submitted.placed_timestamp = None

    reject_data = {"status": "REJECTED", "reason": "Insufficient funds"}

    with patch("asyncio.create_subprocess_exec", return_value=_cli_response(reject_data)):
        await broker._poll_order_status("ORD1", submitted)

    assert len(events) == 1
    assert events[0].data.status == OrderStatus.REJECTED
    assert events[0].data.reject_reason == "Insufficient funds"


@pytest.mark.asyncio
async def test_poll_timeout_emits_rejected_with_poll_timeout():
    broker = _broker(poll_timeout_seconds=0.05, poll_interval_seconds=0.01, poll_max_attempts=2)
    broker._connected = True
    events = []
    broker.events.subscribe(BrokerEvent.ORDER_REJECTED, lambda p: events.append(p))

    submitted = MagicMock()
    submitted.symbol           = "AAPL"
    submitted.order_type       = OrderType.MARKET
    submitted.side             = OrderSide.BUY
    submitted.quantity         = 5.0
    submitted.price            = 0.0
    submitted.placed_timestamp = None

    with patch("asyncio.create_subprocess_exec",
               return_value=_cli_response({"status": "PENDING"})):
        await broker._poll_order_status("ORD1", submitted)

    assert any(e.data.reject_reason == "POLL_TIMEOUT" for e in events)


# ── _map_position ─────────────────────────────────────────────────────────────

def test_map_position_unrealized_pnl():
    broker = _broker()
    data = {"name": "AAPL", "isin": "US0378",
            "quantity": 10.0, "fifo_price": 180.0, "quote_mid_price": 190.0, "valuation": 1900.0}
    pos = broker._map_position(data)
    assert pos.unrealized_pnl == pytest.approx(100.0)
    assert pos.market_value   == pytest.approx(1900.0)


def test_map_position_id_is_isin():
    broker = _broker()
    data = {"name": "AAPL", "isin": "US0378331005",
            "quantity": 5.0, "fifo_price": 100.0, "quote_mid_price": 105.0, "valuation": 525.0}
    pos = broker._map_position(data)
    assert pos.id == "US0378331005"


def test_map_position_symbol_is_company_name():
    """Scalable stores positions by company name, not ticker."""
    broker = _broker()
    data = {"name": "Netflix", "isin": "US64110W1027",
            "quantity": 50.0, "fifo_price": 600.0, "quote_mid_price": 700.0, "valuation": 35000.0}
    pos = broker._map_position(data)
    assert pos.symbol == "Netflix"
    assert pos.id == "US64110W1027"


# ── _map_order_data ───────────────────────────────────────────────────────────

def test_map_order_filled():
    broker = _broker()
    data = {"id": "O1", "description": "AAPL", "side": "BUY",
            "quantity": 10.0, "amount": 1860.0, "status": "SETTLED"}
    order = broker._map_order_data(data)
    assert order.status             == OrderStatus.FILLED
    assert order.average_fill_price == pytest.approx(186.0)


def test_map_order_rejected():
    broker = _broker()
    data = {"orderId": "O2", "symbol": "MSFT", "direction": "BUY",
            "quantity": 5.0, "price": 400.0, "status": "REJECTED"}
    order = broker._map_order_data(data)
    assert order.status == OrderStatus.REJECTED


def test_map_order_unknown_status_defaults_to_submitted():
    broker = _broker()
    data = {"orderId": "O3", "symbol": "NVDA", "quantity": 1.0, "status": "WEIRD_STATUS"}
    order = broker._map_order_data(data)
    assert order.status == OrderStatus.SUBMITTED


# ── capabilities ──────────────────────────────────────────────────────────────

def test_capabilities_long_only():
    broker = _broker()
    assert broker.capabilities.short_selling     is False
    assert broker.capabilities.bracket_orders    is False
    assert broker.capabilities.fractional_shares is False
    assert broker.capabilities.stock_trading     is True
    assert broker.capabilities.knock_out_trading is True
    assert broker.supports_fractional_shares     is False
