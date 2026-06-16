"""
Tests for the Telegram trading bot interface.

Coverage:
    - arg_parser.parse() / require_args()
    - SessionContext / SessionManager
    - CommandHandler (all commands, mocked brokers)
    - formatters — legacy Markdown + new MarkdownV2
    - TelegramNotifier (push events)
    - TelegramBot.attach_broker (legacy)
    - TradingBot._register_commands guard

No real Telegram API calls — everything is mocked.
"""
from __future__ import annotations

import sys
import os
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from core.entities.broker_entities import (
    AccountInfo, Order, OrderSide, OrderStatus, OrderType, TradeSide,
)
from core.entities.position_types import Position

_NOW = dt.datetime(2025, 6, 1, 10, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _order(status=OrderStatus.FILLED, avg_price=185.0, fees=1.5, symbol="AAPL") -> Order:
    return Order(
        id="ORD001", symbol=symbol,
        order_type=OrderType.MARKET, side=OrderSide.BUY,
        quantity=10, price=185.0, status=status,
        placed_timestamp=_NOW, filled_timestamp=_NOW,
        cancelled_timestamp=None,
        average_fill_price=avg_price, fees=fees, leverage=1.0,
    )


def _position(symbol="AAPL", unrealized=250.0) -> Position:
    return Position(
        id="POS001", symbol=symbol, side=TradeSide.LONG,
        open_date=_NOW, close_date=None,
        quantity=10, average_price=180.0, leverage=1.0,
        market_value=1850.0,
        unrealized_pnl=unrealized, unrealized_pnl_percentage=1.39,
        realized_pnl=0.0, realized_pnl_percentage=0.0,
        stop_loss_price=175.0, take_profit_price=200.0,
    )


def _account() -> AccountInfo:
    return AccountInfo(
        account_id="ACC001", account_name="Demo",
        status="ACTIVE", account_type="CFD", currency="USD",
        cash_in_hand=10_000.0, current_value=75_000.0,
        margin_used=5_000.0, margin_available=70_000.0,
        leverage=5.0,
    )


def _mock_update(text: str, chat_id: int = 99) -> MagicMock:
    update              = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id  = chat_id
    return update


def _reply_text(update: MagicMock) -> str:
    """Extract the text sent to reply_text regardless of positional/keyword call."""
    ca = update.message.reply_text.call_args
    if ca.args:
        return ca.args[0]
    return ca.kwargs.get("text", "")


def _mock_broker(account=None, positions=None, orders=None) -> MagicMock:
    broker                          = MagicMock()
    broker.get_account_info         = AsyncMock(return_value=account or _account())
    broker.get_positions            = AsyncMock(return_value=positions or [])
    broker.get_position             = AsyncMock(return_value=None)
    broker.get_orders               = AsyncMock(return_value=orders or [])
    broker.place_order              = AsyncMock(return_value=_order())
    broker.close_position           = AsyncMock(return_value=True)
    broker.update_position_stops    = AsyncMock(return_value=True)
    broker.config                   = MagicMock(
        loan_amount=50_000, equity_floor=55_000,
        hard_max_loss=20_000, starting_equity=122_562,
    )
    return broker


# ─────────────────────────────────────────────────────────────────────────────
# arg_parser
# ─────────────────────────────────────────────────────────────────────────────

from interfaces.telegram.arg_parser import parse, require_args, ParsedCommand


class TestArgParser:
    def test_simple_command(self):
        p = parse("/buy AAPL 10")
        assert p.positional == ["AAPL", "10"]
        assert p.broker  is None
        assert p.account is None

    def test_broker_flag(self):
        p = parse("/buy AAPL 10 --broker ibkr_live")
        assert p.broker      == "ibkr_live"
        assert p.positional  == ["AAPL", "10"]

    def test_account_flag(self):
        p = parse("/buy AAPL 10 --account DU123456")
        assert p.account    == "DU123456"
        assert p.positional == ["AAPL", "10"]

    def test_both_flags_in_order(self):
        p = parse("/buy AAPL 10 --broker ibkr_live --account DU123456")
        assert p.broker      == "ibkr_live"
        assert p.account     == "DU123456"
        assert p.positional  == ["AAPL", "10"]

    def test_both_flags_reversed(self):
        p = parse("/buy AAPL 10 --account DU123456 --broker ibkr_live")
        assert p.broker      == "ibkr_live"
        assert p.account     == "DU123456"
        assert p.positional  == ["AAPL", "10"]

    def test_broker_lowercased(self):
        p = parse("/status --broker Capital_LIVE")
        assert p.broker == "capital_live"

    def test_account_preserves_case(self):
        p = parse("/status --account DU123456")
        assert p.account == "DU123456"

    def test_no_args_command(self):
        p = parse("/status")
        assert p.positional == []

    def test_use_command(self):
        p = parse("/use capital_demo DU999")
        assert p.positional == ["capital_demo", "DU999"]

    def test_require_args_sufficient(self):
        p = ParsedCommand(positional=["AAPL", "10"], broker=None, account=None, raw="")
        assert require_args(p, 2, "/buy SYMBOL QTY") is None

    def test_require_args_insufficient(self):
        p = ParsedCommand(positional=["AAPL"], broker=None, account=None, raw="")
        err = require_args(p, 2, "/buy SYMBOL QTY")
        assert err is not None
        assert "Missing" in err

    def test_require_args_exact(self):
        p = ParsedCommand(positional=["AAPL"], broker=None, account=None, raw="")
        assert require_args(p, 1, "/close SYMBOL") is None

    def test_flags_stripped_from_positional(self):
        p = parse("/buy AAPL 10 --broker capital_live --account DU1")
        assert "--broker"   not in p.positional
        assert "--account"  not in p.positional
        assert "capital_live" not in p.positional


# ─────────────────────────────────────────────────────────────────────────────
# SessionContext / SessionManager
# ─────────────────────────────────────────────────────────────────────────────

from interfaces.telegram.session import SessionContext, SessionManager


class TestSessionContext:
    def test_default_state(self):
        ctx = SessionContext(chat_id=1)
        assert ctx.active_broker  == ""
        assert ctx.active_account == ""

    def test_set_broker_and_account(self):
        ctx = SessionContext(chat_id=1)
        ctx.set("IBKR_Live", "DU123456")
        assert ctx.active_broker  == "ibkr_live"
        assert ctx.active_account == "DU123456"

    def test_set_broker_only(self):
        ctx = SessionContext(chat_id=1)
        ctx.set("capital_demo")
        assert ctx.active_broker  == "capital_demo"
        assert ctx.active_account == ""

    def test_clear(self):
        ctx = SessionContext(chat_id=1)
        ctx.set("ibkr_live", "DU1")
        ctx.clear()
        assert ctx.active_broker  == ""
        assert ctx.active_account == ""
        assert ctx.set_at is None

    def test_describe_no_broker(self):
        ctx = SessionContext(chat_id=1)
        assert "No broker" in ctx.describe()

    def test_describe_with_broker(self):
        ctx = SessionContext(chat_id=1)
        ctx.set("capital_live")
        assert "capital_live" in ctx.describe()

    def test_describe_with_account(self):
        ctx = SessionContext(chat_id=1)
        ctx.set("ibkr_live", "DU123456")
        desc = ctx.describe()
        assert "ibkr_live"  in desc
        assert "DU123456"   in desc

    def test_set_records_timestamp(self):
        ctx = SessionContext(chat_id=1)
        ctx.set("etoro")
        assert ctx.set_at is not None


class TestSessionManager:
    def test_creates_context_on_first_get(self):
        sm = SessionManager(default_broker="capital")
        ctx = sm.get(chat_id=42)
        assert ctx.chat_id       == 42
        assert ctx.active_broker == "capital"

    def test_returns_same_context_on_second_get(self):
        sm  = SessionManager()
        c1  = sm.get(1)
        c1.set("ibkr_live")
        c2  = sm.get(1)
        assert c2.active_broker == "ibkr_live"

    def test_separate_contexts_per_chat(self):
        sm = SessionManager()
        sm.get(1).set("ibkr_live")
        sm.get(2).set("capital_demo")
        assert sm.get(1).active_broker == "ibkr_live"
        assert sm.get(2).active_broker == "capital_demo"

    def test_resolve_flag_wins(self):
        sm = SessionManager(default_broker="capital")
        sm.get(1).set("capital_live")
        broker, account = sm.resolve(1, flag_broker="ibkr_demo", flag_account="DU1")
        assert broker  == "ibkr_demo"
        assert account == "DU1"

    def test_resolve_session_wins_over_default(self):
        sm = SessionManager(default_broker="capital")
        sm.get(1).set("etoro")
        broker, _ = sm.resolve(1)
        assert broker == "etoro"

    def test_resolve_default_when_no_session(self):
        sm = SessionManager(default_broker="capital_live")
        broker, _ = sm.resolve(99, config_default="capital_live")
        assert broker == "capital_live"

    def test_resolve_account_from_flag(self):
        sm = SessionManager()
        _, account = sm.resolve(1, flag_account="DU999")
        assert account == "DU999"

    def test_resolve_account_from_session(self):
        sm = SessionManager()
        sm.get(1).set("ibkr_live", "DU123")
        _, account = sm.resolve(1)
        assert account == "DU123"


# ─────────────────────────────────────────────────────────────────────────────
# CommandHandler
# ─────────────────────────────────────────────────────────────────────────────

from interfaces.telegram.commands import CommandHandler


def _make_handler(brokers=None, **kwargs):
    sessions = SessionManager(default_broker="capital")
    b = brokers or {"capital": _mock_broker()}
    return CommandHandler(brokers=b, sessions=sessions, **kwargs)


def _ctx():
    return MagicMock()


class TestCommandHandlerUse:
    @pytest.mark.asyncio
    async def test_use_sets_broker(self):
        handler = _make_handler(brokers={"capital": _mock_broker(), "ibkr": _mock_broker()})
        update  = _mock_update("/use ibkr")
        await handler.use(update, _ctx())
        update.message.reply_text.assert_awaited_once()
        text = _reply_text(update)
        assert "ibkr" in text

    @pytest.mark.asyncio
    async def test_use_sets_broker_and_account(self):
        handler = _make_handler(brokers={"ibkr_live": _mock_broker()})
        update  = _mock_update("/use ibkr_live DU123456")
        await handler.use(update, _ctx())
        session = handler._sessions.get(update.effective_chat.id)
        assert session.active_broker  == "ibkr_live"
        assert session.active_account == "DU123456"

    @pytest.mark.asyncio
    async def test_use_unknown_broker_errors(self):
        handler = _make_handler()
        update  = _mock_update("/use nonexistent")
        await handler.use(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_use_no_args_shows_context(self):
        handler = _make_handler()
        update  = _mock_update("/use")
        await handler.use(update, _ctx())
        text = _reply_text(update)
        assert "context" in text.lower() or "broker" in text.lower()


class TestCommandHandlerContext:
    @pytest.mark.asyncio
    async def test_context_shows_current_state(self):
        handler = _make_handler()
        handler._sessions.get(99).set("capital")
        update  = _mock_update("/context")
        await handler.context(update, _ctx())
        text = _reply_text(update)
        assert "capital" in text


class TestCommandHandlerStatus:
    @pytest.mark.asyncio
    async def test_status_calls_get_account_info(self):
        broker  = _mock_broker()
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/status")
        await handler.status(update, _ctx())
        broker.get_account_info.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_status_unknown_broker_replies_error(self):
        handler = _make_handler()
        update  = _mock_update("/status --broker ghost")
        await handler.status(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_status_broker_exception_replies_error(self):
        broker = _mock_broker()
        broker.get_account_info = AsyncMock(side_effect=RuntimeError("timeout"))
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/status")
        await handler.status(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_status_flag_overrides_session(self):
        broker_a = _mock_broker()
        broker_b = _mock_broker()
        handler  = _make_handler(brokers={"capital": broker_a, "ibkr": broker_b})
        handler._sessions.get(99).set("capital")
        update   = _mock_update("/status --broker ibkr")
        await handler.status(update, _ctx())
        broker_b.get_account_info.assert_awaited_once()
        broker_a.get_account_info.assert_not_awaited()


class TestCommandHandlerPositions:
    @pytest.mark.asyncio
    async def test_positions_no_positions(self):
        handler = _make_handler()
        update  = _mock_update("/positions")
        await handler.positions(update, _ctx())
        text = _reply_text(update)
        assert "No open" in text

    @pytest.mark.asyncio
    async def test_positions_with_positions(self):
        broker  = _mock_broker(positions=[_position("AAPL")])
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/positions")
        await handler.positions(update, _ctx())
        text = _reply_text(update)
        assert "AAPL" in text

    @pytest.mark.asyncio
    async def test_positions_passes_symbol_filter(self):
        broker  = _mock_broker()
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/positions TSLA")
        await handler.positions(update, _ctx())
        broker.get_positions.assert_awaited_once_with(symbol="TSLA")


class TestCommandHandlerOrders:
    @pytest.mark.asyncio
    async def test_orders_no_pending(self):
        handler = _make_handler()
        update  = _mock_update("/orders")
        await handler.orders(update, _ctx())
        text = _reply_text(update)
        assert "No pending" in text

    @pytest.mark.asyncio
    async def test_orders_pending_shown(self):
        pending = _order(status=OrderStatus.SUBMITTED, symbol="TSLA")
        broker  = _mock_broker(orders=[pending])
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/orders")
        await handler.orders(update, _ctx())
        text = _reply_text(update)
        assert "TSLA" in text


class TestCommandHandlerPnl:
    @pytest.mark.asyncio
    async def test_pnl_no_monitor(self):
        handler = _make_handler()
        update  = _mock_update("/pnl")
        await handler.pnl(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_pnl_with_monitor(self):
        rm              = MagicMock()
        rm.daily_pnl    = 1234.56
        rm.daily_trades = 5
        rm.daily_wins   = 3
        rm.daily_losses = 2
        rm.win_rate     = 0.6
        handler         = _make_handler(risk_monitor=rm)
        update          = _mock_update("/pnl")
        await handler.pnl(update, _ctx())
        text = _reply_text(update)
        assert "1,234" in text


class TestCommandHandlerProgress:
    @pytest.mark.asyncio
    async def test_progress_no_tracker(self):
        handler = _make_handler()
        update  = _mock_update("/progress")
        await handler.progress(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_progress_with_tracker(self):
        ct                  = MagicMock()
        ct.current_equity   = 130_000
        ct.starting_equity  = 122_562
        ct.target_equity    = 1_000_000
        ct.daily_target_pct = 0.01
        ct.days_completed   = 10
        ct.effective_days   = 252
        ct.session_pnl      = 500.0
        handler             = _make_handler(compound_tracker=ct)
        update              = _mock_update("/progress")
        await handler.progress(update, _ctx())
        text = _reply_text(update)
        assert "Progress" in text or "Equity" in text


class TestCommandHandlerRisk:
    @pytest.mark.asyncio
    async def test_risk_no_monitor(self):
        handler = _make_handler()
        update  = _mock_update("/risk")
        await handler.risk(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_risk_with_monitor(self):
        rm                    = MagicMock()
        rm.daily_pnl          = -500
        rm._daily_loss_limit  = 2000
        handler               = _make_handler(risk_monitor=rm)
        update                = _mock_update("/risk")
        await handler.risk(update, _ctx())
        text = _reply_text(update)
        assert "Risk" in text


class TestCommandHandlerBuySell:
    @pytest.mark.asyncio
    async def test_buy_places_order(self):
        broker  = _mock_broker()
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/buy AAPL 10")
        await handler.buy(update, _ctx())
        broker.place_order.assert_awaited_once()
        _, kwargs = broker.place_order.call_args
        assert kwargs["symbol"]   == "AAPL"
        assert kwargs["quantity"] == 10.0

    @pytest.mark.asyncio
    async def test_sell_places_order(self):
        broker  = _mock_broker()
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/sell TSLA 5")
        await handler.sell(update, _ctx())
        broker.place_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_buy_missing_args_returns_error(self):
        handler = _make_handler()
        update  = _mock_update("/buy AAPL")
        await handler.buy(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text
        assert "Missing" in text

    @pytest.mark.asyncio
    async def test_buy_invalid_qty_returns_error(self):
        handler = _make_handler()
        update  = _mock_update("/buy AAPL notanumber")
        await handler.buy(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_buy_with_broker_flag(self):
        broker_cap  = _mock_broker()
        broker_ibkr = _mock_broker()
        handler     = _make_handler(brokers={"capital": broker_cap, "ibkr": broker_ibkr})
        update      = _mock_update("/buy AAPL 10 --broker ibkr")
        await handler.buy(update, _ctx())
        broker_ibkr.place_order.assert_awaited_once()
        broker_cap.place_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_buy_broker_exception_replies_error(self):
        broker = _mock_broker()
        broker.place_order = AsyncMock(side_effect=RuntimeError("insufficient funds"))
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/buy AAPL 10")
        await handler.buy(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text


class TestCommandHandlerClose:
    @pytest.mark.asyncio
    async def test_close_missing_symbol(self):
        handler = _make_handler()
        update  = _mock_update("/close")
        await handler.close(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_close_no_position(self):
        handler = _make_handler()
        update  = _mock_update("/close AAPL")
        await handler.close(update, _ctx())
        text = _reply_text(update)
        assert "No open position" in text

    @pytest.mark.asyncio
    async def test_close_success(self):
        broker = _mock_broker()
        broker.get_position = AsyncMock(return_value=_position("AAPL"))
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/close AAPL")
        await handler.close(update, _ctx())
        broker.close_position.assert_awaited_once()
        text = _reply_text(update)
        assert "✅" in text

    @pytest.mark.asyncio
    async def test_close_broker_returns_false(self):
        broker = _mock_broker()
        broker.get_position   = AsyncMock(return_value=_position("AAPL"))
        broker.close_position = AsyncMock(return_value=False)
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/close AAPL")
        await handler.close(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text


class TestCommandHandlerCloseAll:
    @pytest.mark.asyncio
    async def test_closeall_no_positions(self):
        handler = _make_handler()
        update  = _mock_update("/closeall")
        await handler.closeall(update, _ctx())
        text = _reply_text(update)
        assert "No open" in text

    @pytest.mark.asyncio
    async def test_closeall_closes_all(self):
        broker  = _mock_broker(positions=[_position("AAPL"), _position("TSLA")])
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/closeall")
        await handler.closeall(update, _ctx())
        assert broker.close_position.await_count == 2

    @pytest.mark.asyncio
    async def test_closeall_partial_failure_shown(self):
        pos_a  = _position("AAPL")
        pos_b  = _position("TSLA")
        broker = _mock_broker(positions=[pos_a, pos_b])
        broker.close_position = AsyncMock(side_effect=[True, RuntimeError("rejected")])
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/closeall")
        await handler.closeall(update, _ctx())
        text = _reply_text(update)
        assert "AAPL" in text
        assert "TSLA" in text


class TestCommandHandlerStop:
    @pytest.mark.asyncio
    async def test_stop_missing_args(self):
        handler = _make_handler()
        update  = _mock_update("/stop AAPL")
        await handler.stop_cmd(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_stop_no_position(self):
        handler = _make_handler()
        update  = _mock_update("/stop AAPL 180.0")
        await handler.stop_cmd(update, _ctx())
        text = _reply_text(update)
        assert "No open position" in text

    @pytest.mark.asyncio
    async def test_stop_success(self):
        broker = _mock_broker()
        broker.get_position = AsyncMock(return_value=_position("AAPL"))
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/stop AAPL 175.50")
        await handler.stop_cmd(update, _ctx())
        broker.update_position_stops.assert_awaited_once()
        text = _reply_text(update)
        assert "✅" in text

    @pytest.mark.asyncio
    async def test_stop_not_supported(self):
        broker = _mock_broker()
        broker.get_position           = AsyncMock(return_value=_position("AAPL"))
        broker.update_position_stops  = AsyncMock(return_value=False)
        handler = _make_handler(brokers={"capital": broker})
        update  = _mock_update("/stop AAPL 175.50")
        await handler.stop_cmd(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text


class TestCommandHandlerHaltResume:
    def _strategy(self):
        s = MagicMock()
        s._symbol = "AAPL"
        return s

    @pytest.mark.asyncio
    async def test_halt_all_strategies(self):
        s1 = self._strategy()
        s2 = self._strategy()
        handler = _make_handler(strategies={"strat_1": s1, "strat_2": s2})
        update  = _mock_update("/halt")
        await handler.halt(update, _ctx())
        s1.halt.assert_called_once()
        s2.halt.assert_called_once()

    @pytest.mark.asyncio
    async def test_halt_specific_symbol(self):
        s1 = MagicMock(); s1._symbol = "AAPL"
        s2 = MagicMock(); s2._symbol = "TSLA"
        handler = _make_handler(strategies={"aapl_strat": s1, "tsla_strat": s2})
        update  = _mock_update("/halt AAPL")
        await handler.halt(update, _ctx())
        s1.halt.assert_called_once()
        s2.halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_halt_no_match(self):
        handler = _make_handler(strategies={})
        update  = _mock_update("/halt AAPL")
        await handler.halt(update, _ctx())
        text = _reply_text(update)
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_resume_all_strategies(self):
        s1 = self._strategy()
        s2 = self._strategy()
        handler = _make_handler(strategies={"strat_1": s1, "strat_2": s2})
        update  = _mock_update("/resume")
        await handler.resume(update, _ctx())
        s1.resume.assert_called_once()
        s2.resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_specific_by_strategy_id(self):
        s1 = MagicMock(); s1._symbol = "AAPL"
        s2 = MagicMock(); s2._symbol = "TSLA"
        handler = _make_handler(strategies={"TSLA_STRAT": s1, "other": s2})
        update  = _mock_update("/resume TSLA_STRAT")
        await handler.resume(update, _ctx())
        s1.resume.assert_called_once()
        s2.resume.assert_not_called()


class TestCommandHandlerHelp:
    @pytest.mark.asyncio
    async def test_help_lists_brokers(self):
        handler = _make_handler(brokers={"capital_live": _mock_broker(), "ibkr_demo": _mock_broker()})
        update  = _mock_update("/help")
        await handler.help(update, _ctx())
        text = _reply_text(update)
        assert "capital" in text
        assert "ibkr" in text


# ─────────────────────────────────────────────────────────────────────────────
# Formatters — legacy Markdown
# ─────────────────────────────────────────────────────────────────────────────

from interfaces.telegram.formatters import (
    fmt_account,
    fmt_connection_lost,
    fmt_order,
    fmt_position,
    fmt_reconnecting,
    fmt_risk_alert,
)


class TestLegacyFormatters:
    def test_fmt_order_filled(self):
        msg = fmt_order(_order(status=OrderStatus.FILLED))
        assert "FILLED" in msg and "AAPL" in msg and "✅" in msg

    def test_fmt_order_rejected(self):
        assert "❌" in fmt_order(_order(status=OrderStatus.REJECTED))

    def test_fmt_order_cancelled(self):
        assert "🚫" in fmt_order(_order(status=OrderStatus.CANCELLED))

    def test_fmt_order_with_enter_reason(self):
        o = _order(); o.enter_reason = "breakout"
        assert "breakout" in fmt_order(o)

    def test_fmt_order_with_reject_reason(self):
        o = _order(status=OrderStatus.REJECTED); o.reject_reason = "Insufficient margin"
        assert "Insufficient margin" in fmt_order(o)

    def test_fmt_position_profit_emoji(self):
        msg = fmt_position(_position(unrealized=250.0))
        assert "📈" in msg and "AAPL" in msg

    def test_fmt_position_loss_emoji(self):
        assert "📉" in fmt_position(_position(unrealized=-100.0))

    def test_fmt_position_contains_sl_tp(self):
        msg = fmt_position(_position())
        assert "175.00" in msg and "200.00" in msg

    def test_fmt_account_key_fields(self):
        msg = fmt_account(_account())
        assert "75,000.00" in msg and "Demo" in msg and "USD" in msg

    def test_fmt_risk_equity_floor(self):
        msg = fmt_risk_alert("equity_floor_hit", {"own_equity": 54_000, "equity_floor": 55_000, "loan_amount": 50_000})
        assert "EQUITY FLOOR" in msg and "54,000" in msg and "🚨" in msg

    def test_fmt_risk_daily_loss(self):
        msg = fmt_risk_alert("daily_loss_limit", {"drawdown": 20_500, "hard_max_loss": 20_000, "current_equity": 102_000})
        assert "DAILY LOSS" in msg and "20,500" in msg and "🔴" in msg

    def test_fmt_risk_unknown_event(self):
        msg = fmt_risk_alert("some_other_event", {"key": "val"})
        assert "SOME_OTHER_EVENT" in msg and "⚠️" in msg

    def test_fmt_connection_lost(self):
        msg = fmt_connection_lost("ibkr_demo")
        assert "CONNECTION LOST" in msg and "ibkr_demo" in msg and "⚠️" in msg

    def test_fmt_reconnecting(self):
        msg = fmt_reconnecting("capital_live", attempt=2, max_attempts=3)
        assert "2/3" in msg and "capital_live" in msg


# ─────────────────────────────────────────────────────────────────────────────
# Formatters — MarkdownV2
# ─────────────────────────────────────────────────────────────────────────────

from interfaces.telegram.formatters import (
    _esc,
    v2_account_status,
    v2_context_set,
    v2_error,
    v2_help_text,
    v2_order_placed,
    v2_orders_list,
    v2_positions_list,
    v2_risk_status,
    v2_success,
)


class TestMarkdownV2Formatters:
    def test_esc_escapes_dot(self):
        assert r"\." in _esc("1.0")

    def test_esc_escapes_dash(self):
        assert r"\-" in _esc("stop-loss")

    def test_esc_escapes_parens(self):
        assert r"\(" in _esc("(value)")

    def test_v2_error_contains_cross(self):
        assert "❌" in v2_error("something failed")

    def test_v2_success_contains_tick(self):
        assert "✅" in v2_success("order sent")

    def test_v2_context_set_shows_broker(self):
        msg = v2_context_set("ibkr_live", "DU123")
        assert "ibkr" in msg and "DU123" in msg

    def test_v2_context_set_no_account(self):
        msg = v2_context_set("capital", "")
        assert "capital" in msg
        assert "DU" not in msg

    def test_v2_account_status_contains_fields(self):
        msg = v2_account_status(_account(), "capital", "ACC001")
        assert "75" in msg and "capital" in msg

    def test_v2_positions_list_empty(self):
        msg = v2_positions_list([], "capital", "")
        assert "No open" in msg

    def test_v2_positions_list_with_position(self):
        msg = v2_positions_list([_position("AAPL")], "capital", "ACC1")
        assert "AAPL" in msg

    def test_v2_orders_list_no_pending(self):
        msg = v2_orders_list([_order(status=OrderStatus.FILLED)], "capital")
        assert "No pending" in msg

    def test_v2_orders_list_pending_shown(self):
        msg = v2_orders_list([_order(status=OrderStatus.SUBMITTED, symbol="TSLA")], "capital")
        assert "TSLA" in msg

    def test_v2_order_placed(self):
        msg = v2_order_placed(_order(), "ibkr_live")
        assert "AAPL" in msg and "ibkr" in msg

    def test_v2_risk_status_all_ok(self):
        msg = v2_risk_status(
            daily_pnl=100, daily_loss_limit=2000, equity_floor=55_000,
            own_equity=80_000, hard_max_loss=20_000,
            starting_equity=122_562, current_equity=120_000,
        )
        assert "✅" in msg

    def test_v2_risk_status_breach_shown(self):
        msg = v2_risk_status(
            daily_pnl=-2100, daily_loss_limit=2000, equity_floor=55_000,
            own_equity=40_000, hard_max_loss=20_000,
            starting_equity=122_562, current_equity=80_000,
        )
        assert "🚨" in msg or "⚠️" in msg

    def test_v2_help_lists_brokers(self):
        msg = v2_help_text(["capital_live", "ibkr_demo"])
        assert "capital" in msg and "ibkr" in msg


# ─────────────────────────────────────────────────────────────────────────────
# TelegramNotifier
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def notifier():
    with patch("telegram.Bot"):
        from interfaces.telegram.notifier import TelegramNotifier
        return TelegramNotifier(token="test-token", chat_id="12345")


class TestTelegramNotifier:
    @pytest.mark.asyncio
    async def test_on_order_sends_message(self, notifier):
        notifier._bot.send_message = AsyncMock()
        payload = MagicMock(); payload.data = _order()
        await notifier.on_order(payload)
        notifier._bot.send_message.assert_awaited_once()
        assert "AAPL" in notifier._bot.send_message.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_on_order_ignores_non_order(self, notifier):
        notifier._bot.send_message = AsyncMock()
        payload = MagicMock(); payload.data = "not an order"
        await notifier.on_order(payload)
        notifier._bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_position_sends_message(self, notifier):
        notifier._bot.send_message = AsyncMock()
        payload = MagicMock(); payload.data = _position()
        await notifier.on_position(payload)
        notifier._bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_risk_equity_floor(self, notifier):
        notifier._bot.send_message = AsyncMock()
        payload = MagicMock()
        payload.event.value = "equity_floor_hit"
        payload.data = {"own_equity": 54_000, "equity_floor": 55_000, "loan_amount": 50_000}
        await notifier.on_risk(payload)
        assert "EQUITY FLOOR" in notifier._bot.send_message.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_on_connection_lost(self, notifier):
        notifier._bot.send_message = AsyncMock()
        payload = MagicMock()
        payload.event.value = "connection_lost"
        payload.broker_id = "ibkr_demo"
        payload.data = {}
        await notifier.on_connection(payload)
        assert "CONNECTION LOST" in notifier._bot.send_message.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_on_reconnecting(self, notifier):
        notifier._bot.send_message = AsyncMock()
        payload = MagicMock()
        payload.event.value = "reconnecting"
        payload.broker_id = "ibkr_demo"
        payload.data = {"attempt": 1, "max_attempts": 3}
        await notifier.on_connection(payload)
        assert "1/3" in notifier._bot.send_message.call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_send_failure_does_not_raise(self, notifier):
        notifier._bot.send_message = AsyncMock(side_effect=Exception("network error"))
        payload = MagicMock()
        payload.event.value = "connection_lost"
        payload.broker_id = "ibkr"; payload.data = {}
        await notifier.on_connection(payload)   # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# TelegramBot (legacy) — attach_broker
# ─────────────────────────────────────────────────────────────────────────────

class TestTelegramBotAttachBroker:
    def test_attach_broker_subscribes_to_events(self):
        mock_app          = MagicMock()
        mock_app.bot_data = {}
        mock_builder      = MagicMock()
        mock_builder.return_value.token.return_value.build.return_value = mock_app

        with patch("interfaces.telegram.bot.Application") as mock_application, \
             patch("interfaces.telegram.bot.TelegramNotifier"):
            mock_application.builder = mock_builder

            from interfaces.telegram.bot import TelegramBot
            bot    = TelegramBot(token="tok", chat_id="123")
            broker = MagicMock()
            broker.broker_id = "test_broker"
            broker.events    = MagicMock()

            bot.attach_broker(broker)

            assert broker.events.subscribe.call_count >= 8
            assert mock_app.bot_data["broker"] is broker


# ─────────────────────────────────────────────────────────────────────────────
# TradingBot — chat_id guard
# ─────────────────────────────────────────────────────────────────────────────

class TestTradingBotGuard:
    def _build_bot(self, chat_id=12345):
        mock_app     = MagicMock()
        mock_builder = MagicMock()
        mock_builder.return_value.token.return_value.build.return_value = mock_app

        with patch("interfaces.telegram.bot.Application") as mock_app_cls:
            mock_app_cls.builder = mock_builder
            from interfaces.telegram.bot import TradingBot
            bot = TradingBot(
                token="tok",
                chat_id=chat_id,
                brokers={"capital": _mock_broker()},
            )
        return bot

    def test_from_env_raises_without_token(self):
        from interfaces.telegram.bot import TradingBot
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "123"}):
            with pytest.raises(EnvironmentError, match="TELEGRAM_BOT_TOKEN"):
                with patch("interfaces.telegram.bot.Application"):
                    TradingBot.from_env(brokers={})

    def test_from_env_raises_without_chat_id(self):
        from interfaces.telegram.bot import TradingBot
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "0"}):
            with pytest.raises(EnvironmentError, match="TELEGRAM_CHAT_ID"):
                with patch("interfaces.telegram.bot.Application"):
                    TradingBot.from_env(brokers={})
