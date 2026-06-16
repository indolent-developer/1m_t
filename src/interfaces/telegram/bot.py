"""
interfaces.telegram.bot

Two bot classes:

TelegramBot (legacy)
    Single-broker bot. Attach one broker via attach_broker().
    Supports the original /scan, /account, /positions, /orders, /risk commands.

TradingBot (new)
    Multi-broker, session-aware bot. Supports the full command set with
    /use, --broker, --account flags and per-chat session context.

    Brokers are passed as a dict:
        {
            "capital_live": CapitalBroker(...),
            "capital_demo": CapitalBroker(..., is_demo=True),
            "ibkr_live":    IBKRBroker(...),
            "ibkr_demo":    IBKRBroker(..., is_demo=True),
            "etoro":        eToroBroker(...),
        }

Security: only messages from the configured TELEGRAM_CHAT_ID are processed.
All other senders are silently ignored.

Setup:
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="987654321"
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from core.utils.log_helper import getLogger as _getLogger, LK, set_log_context
from typing import Any, Dict, Optional

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler as TGCommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:
    sys.exit("Missing dep — run:  pip install python-telegram-bot")

from adapters.brokers.entities.broker_event import BrokerEvent
from interfaces.telegram.commands import (
    CommandHandler,
    HELP_TEXT,
    cmd_account,
    cmd_broker,
    cmd_buy,
    cmd_close,
    cmd_closeall,
    cmd_fills,
    cmd_help,
    cmd_ind,
    cmd_indp,
    cmd_ml,
    cmd_nk,
    cmd_orders,
    cmd_pm,
    cmd_pnl,
    cmd_positions,
    cmd_pre,
    cmd_quote,
    cmd_risk,
    cmd_scan,
    cmd_sell,
    cmd_sp,
    cmd_start,
    cmd_stop_loss,
    cmd_vol,
)
from interfaces.telegram.notifier import TelegramNotifier
from interfaces.telegram.session import SessionManager

logger = _getLogger(__name__, app_name="tg-bot")


# ── Legacy single-broker bot ──────────────────────────────────────────────────

class TelegramBot:
    """
    Original single-broker Telegram bot.
    Wire one broker via attach_broker(); the broker's event bus drives push
    notifications. Use TradingBot for multi-broker support.
    """

    def __init__(self, token: str, chat_id: str | int) -> None:
        self._token   = token
        self._chat_id = str(chat_id)
        self._app     = (
            Application.builder()
            .token(token)
            .build()
        )
        self._notifier = TelegramNotifier(token=token, chat_id=chat_id)
        self._register_commands()

    def _register_commands(self) -> None:
        app = self._app
        # Full names
        app.add_handler(TGCommandHandler("start",     cmd_start))
        app.add_handler(TGCommandHandler("help",      cmd_help))
        app.add_handler(TGCommandHandler("scan",      cmd_scan))
        app.add_handler(TGCommandHandler("account",   cmd_account))
        app.add_handler(TGCommandHandler("positions", cmd_positions))
        app.add_handler(TGCommandHandler("orders",    cmd_orders))
        app.add_handler(TGCommandHandler("risk",      cmd_risk))
        app.add_handler(TGCommandHandler("broker",    cmd_broker))
        app.add_handler(TGCommandHandler("buy",       cmd_buy))
        app.add_handler(TGCommandHandler("sell",      cmd_sell))
        app.add_handler(TGCommandHandler("close",     cmd_close))
        app.add_handler(TGCommandHandler("closeall",  cmd_closeall))
        app.add_handler(TGCommandHandler("pnl",       cmd_pnl))
        app.add_handler(TGCommandHandler("quote",     cmd_quote))
        app.add_handler(TGCommandHandler("ind",       cmd_ind))
        app.add_handler(TGCommandHandler("indp",      cmd_indp))
        app.add_handler(TGCommandHandler("fills",     cmd_fills))
        app.add_handler(TGCommandHandler("ml",        cmd_ml))
        app.add_handler(TGCommandHandler("monitor-levels", cmd_ml))
        # Shortcuts
        app.add_handler(TGCommandHandler("h",   cmd_help))
        app.add_handler(TGCommandHandler("a",   cmd_account))
        app.add_handler(TGCommandHandler("p",   cmd_positions))
        app.add_handler(TGCommandHandler("o",   cmd_orders))
        app.add_handler(TGCommandHandler("r",   cmd_risk))
        app.add_handler(TGCommandHandler("b",   cmd_buy))
        app.add_handler(TGCommandHandler("s",   cmd_sell))
        app.add_handler(TGCommandHandler("c",   cmd_close))
        app.add_handler(TGCommandHandler("ca",  cmd_closeall))
        app.add_handler(TGCommandHandler("sl",  cmd_stop_loss))
        app.add_handler(TGCommandHandler("q",   cmd_quote))
        app.add_handler(TGCommandHandler("bk",  cmd_broker))
        app.add_handler(TGCommandHandler("pm",  cmd_pm))
        app.add_handler(TGCommandHandler("pre", cmd_pre))
        app.add_handler(TGCommandHandler("vol", cmd_vol))
        app.add_handler(TGCommandHandler("sp",  cmd_sp))
        app.add_handler(TGCommandHandler("nk",  cmd_nk))

    def attach_broker(self, broker) -> None:
        """Wire a broker into the bot and subscribe the notifier to its events."""
        self._app.bot_data["broker"] = broker

        bus = broker.events
        bus.subscribe(BrokerEvent.ORDER_FILLED,       self._notifier.on_order)
        bus.subscribe(BrokerEvent.ORDER_REJECTED,     self._notifier.on_order)
        bus.subscribe(BrokerEvent.ORDER_CANCELLED,    self._notifier.on_order)
        bus.subscribe(BrokerEvent.ORDER_PARTIAL_FILL, self._notifier.on_order)
        bus.subscribe(BrokerEvent.POSITION_OPENED,    self._notifier.on_position)
        bus.subscribe(BrokerEvent.POSITION_CLOSED,    self._notifier.on_position)
        bus.subscribe(BrokerEvent.POSITION_UPDATED,   self._notifier.on_position)
        bus.subscribe(BrokerEvent.EQUITY_FLOOR_HIT,   self._notifier.on_risk)
        bus.subscribe(BrokerEvent.DAILY_LOSS_LIMIT,   self._notifier.on_risk)
        bus.subscribe(BrokerEvent.CONNECTION_LOST,    self._notifier.on_connection)
        bus.subscribe(BrokerEvent.RECONNECTING,       self._notifier.on_connection)

        logger.info("[TelegramBot] Attached to broker %s", broker.broker_id)

    async def run(self) -> None:
        """Start polling — blocks until interrupted."""
        import signal
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        logger.info("[TelegramBot] Starting polling …")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("[TelegramBot] Bot is running. Press Ctrl-C to stop.")
        broker = self._app.bot_data.get("broker")
        broker_id = broker.broker_id if broker else "no broker"
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=f"🟢 *Super Ron online* — broker: `{broker_id}`\n\n{HELP_TEXT}",
            parse_mode="Markdown",
        )
        await stop.wait()
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()


# ── Multi-broker session-aware bot ────────────────────────────────────────────

class TradingBot:
    """
    Multi-broker Telegram trading bot with per-chat session context.

    Each command resolves its target broker from:
        1. --broker flag in the command text
        2. /use session context for that chat
        3. default_broker config fallback

    Parameters
    ----------
    token:            Telegram bot token.
    chat_id:          Authorised chat ID — all other senders are silently ignored.
    brokers:          Dict of broker_name → BaseBroker instances.
    default_broker:   Broker used when no session or flag is set.
    risk_monitor:     Optional RiskMonitor for /pnl and /risk.
    compound_tracker: Optional CompoundTracker for /progress.
    strategies:       Optional {strategy_id: strategy} for /halt and /resume.
    """

    def __init__(
        self,
        token:             str,
        chat_id:           int,
        brokers:           Dict[str, Any],
        default_broker:    str = "capital",
        risk_monitor:      Optional[Any] = None,
        compound_tracker:  Optional[Any] = None,
        strategies:        Optional[Dict[str, Any]] = None,
    ) -> None:
        self._token   = token
        self._chat_id = int(chat_id)
        self._brokers = brokers

        self._sessions = SessionManager(default_broker=default_broker)

        self._cmd = CommandHandler(
            brokers=brokers,
            sessions=self._sessions,
            risk_monitor=risk_monitor,
            compound_tracker=compound_tracker,
            strategies=strategies,
            config_default_broker=default_broker,
        )

        self._app = (
            Application.builder()
            .token(token)
            .build()
        )

        self._register_commands()

        self._app.add_handler(
            MessageHandler(filters.COMMAND, self._unknown_command)
        )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        brokers:          Dict[str, Any],
        default_broker:   str = "capital",
        risk_monitor:     Optional[Any] = None,
        compound_tracker: Optional[Any] = None,
        strategies:       Optional[Dict[str, Any]] = None,
    ) -> "TradingBot":
        """Build TradingBot from TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN) and TELEGRAM_CHAT_ID env vars."""
        token   = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

        if not token:
            raise EnvironmentError(
                "TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN) not set. Add it to .env or set as env var."
            )
        if not chat_id:
            raise EnvironmentError(
                "TELEGRAM_CHAT_ID not set. Get yours from @userinfobot and add to .env."
            )

        return cls(
            token=token,
            chat_id=chat_id,
            brokers=brokers,
            default_broker=default_broker,
            risk_monitor=risk_monitor,
            compound_tracker=compound_tracker,
            strategies=strategies,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start long-polling. Blocks until interrupted."""
        import signal
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        logger.info("[TradingBot] Starting (chat_id=%d, brokers=%s)", self._chat_id, list(self._brokers))
        async with self._app:
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(
                allowed_updates=["message"],
                drop_pending_updates=True,
            )
            logger.info("[TradingBot] Ready — listening for commands")
            await stop.wait()
            await self._app.updater.stop()
            await self._app.stop()
        logger.info("[TradingBot] Stopped")

    async def stop(self) -> None:
        """Graceful stop — call from your shutdown sequence."""
        if self._app.updater.running:
            await self._app.updater.stop()

    # ── Command registration ──────────────────────────────────────────────────

    def _register_commands(self) -> None:
        def guarded(handler):
            """Reject messages from unknown chat_ids silently."""
            async def _inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
                if update.effective_chat.id != self._chat_id:
                    logger.warning(
                        "[TradingBot] Rejected unknown chat_id=%d",
                        update.effective_chat.id,
                    )
                    return
                await handler(update, ctx)
            return _inner

        commands = [
            ("start",     self._cmd.help),
            ("help",      self._cmd.help),
            ("use",       self._cmd.use),
            ("context",   self._cmd.context),
            ("status",    self._cmd.status),
            ("positions", self._cmd.positions),
            ("orders",    self._cmd.orders),
            ("quote",     self._cmd.quote),
            ("q",         self._cmd.quote),
            ("ind",       self._cmd.ind),
            ("indp",      self._cmd.indp),
            ("pnl",       self._cmd.pnl),
            ("progress",  self._cmd.progress),
            ("risk",      self._cmd.risk),
            ("buy",       self._cmd.buy),
            ("sell",      self._cmd.sell),
            ("close",     self._cmd.close),
            ("closeall",  self._cmd.closeall),
            ("stop",      self._cmd.stop_cmd),
            ("fills",     self._cmd.fills),
            ("halt",      self._cmd.halt),
            ("resume",    self._cmd.resume),
            ("ml",        self._cmd.monitor_levels),
            ("monitor-levels", self._cmd.monitor_levels),
        ]

        for name, handler in commands:
            self._app.add_handler(TGCommandHandler(name, guarded(handler)))

    async def _unknown_command(
        self,
        update: Update,
        _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if update.effective_chat.id != self._chat_id:
            return
        await update.message.reply_text(
            "Unknown command\\. Use /help to see all commands\\.",
            parse_mode="MarkdownV2",
        )


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    from adapters.brokers.scalable_broker import ScalableBroker
    from core.config.config_models import ScalableBrokerConfig

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )

    _token   = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    _chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not _token or not _chat_id:
        sys.exit("Set TELEGRAM_BOT_TOKEN (or TELEGRAM_TOKEN) and TELEGRAM_CHAT_ID environment variables.")

    async def _main() -> None:
        _broker = ScalableBroker(ScalableBrokerConfig(readonly=False))
        ok = await _broker.connect()
        if not ok:
            sys.exit("[scalable] connect() failed — ensure 'sc' CLI is installed and run 'sc login' first.")
        _bot = TelegramBot(token=_token, chat_id=_chat_id)
        _bot.attach_broker(_broker)
        try:
            await _bot.run()
        finally:
            await _broker.disconnect()

    asyncio.run(_main())
