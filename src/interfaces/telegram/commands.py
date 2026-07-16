"""
interfaces.telegram.commands — Telegram command handlers.

Each handler is a plain async function that receives a Telegram Update and
context. Handlers are registered on the Application in bot.py.

Commands:
    /start        — welcome
    /help         — list commands
    /scan <type>  — run a scanner (pm | pre | vol | spikes)
    /positions    — open positions
    /orders       — recent orders
    /account      — account snapshot
    /risk         — risk metrics vs limits
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from core.utils.log_helper import getLogger as _lh_getLogger, LK, set_log_context
from services.command_service import CommandService, CommandError
from services.position_service import find_position as _find_position, get_position_for_ticker as _get_position_for_ticker
from services.pnl_service import get_fills as _get_fills, calc_pnl as _calc_pnl

logger = _lh_getLogger(__name__, app_name="tg-bot")

_ML_LEVELS_PATH = Path(__file__).resolve().parents[3] / "data" / "ml_levels.json"


# ── Level monitor persistence ─────────────────────────────────────────────────

def _load_ml_levels() -> dict:
    if _ML_LEVELS_PATH.exists():
        try:
            return json.loads(_ML_LEVELS_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_ml_levels(data: dict) -> None:
    _ML_LEVELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ML_LEVELS_PATH.write_text(json.dumps(data, indent=2))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HELP_TEXT = """
*Super Ron — Trading Bot*

*Scanners*
/pm  /scan pm        — Post-market movers (>3% after hours)
/pre /scan pre       — Pre-market movers (>5% before open)
/vol /scan vol       — Daily high volumes (smart rel vol)
/sp  /scan spikes    — Intraday spikes (>2% from open)
/nk  /scan parabolic — NK-parabolic (1M>30%, RSI>60, RelVol>1.5)

*Account*
/a   /account    — Account snapshot (equity, margin)
/p   /positions [csv] — Open positions (add csv for copyable format)
/o   /orders     — Recent orders
/r   /risk       — Risk metrics vs limits
/pnl             — Open P&L across positions
/q   /quote SYMBOL — Bid, ask, last price for a symbol
/ind  SYMBOL [TF]  — Indicators: ATR, RSI, EMA 8/20, SuperTrend  (TF: 1m 5m 15m 1h 1d)
/indp [TF]         — Portfolio indicators (default 1m +ext) | ignore/unignore/list
/fills SYMBOL [N]  — Fill history + realized/unrealized P&L (last N trades)

*Level Monitor*
/ml  SYMBOL LEVEL [LEVEL ...] [filter ...]
     Filters: break\_up  break\_down  bounce  reject  (default: all)
     /ml APLD 41.5 break\_down      — alert when APLD breaks below 41.5
     /ml AAPL 200 210 bounce       — alert on bounces at 200 and 210
     /ml status [SYMBOL]           — current price + distance from watched levels
     /ml stop SYMBOL               — stop monitoring
     /ml list                      — list active monitors

*Trading*
/b   /buy SYMBOL QTY   — Market buy
/s   /sell SYMBOL QTY  — Market sell
/c   /close SYMBOL     — Close position
/ca  /closeall         — Close all positions
/sl  SYMBOL PRICE      — Update stop-loss

*Broker*
/bk  /broker     — Show connected broker
/bk list         — List available brokers

/h   /help       — Show this message
""".strip()


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"👋 *Super Ron is online.*\n\nType /help to see available commands.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_broker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args   = context.args
    broker = context.bot_data.get("broker")

    if args and args[0].lower() == "list":
        brokers = context.bot_data.get("brokers", {})
        if not brokers:
            # Single-broker mode — show the one attached broker
            if broker:
                await update.message.reply_text(
                    f"*Available brokers:*\n• `{broker.broker_id}` ✅ _(active)_",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text("❌ No broker connected.")
        else:
            active = context.bot_data.get("active_broker", "")
            lines  = [
                f"• `{name}`{' ✅ _(active)_' if name == active else ''}"
                for name in brokers
            ]
            await update.message.reply_text(
                "*Available brokers:*\n" + "\n".join(lines),
                parse_mode="Markdown",
            )
        return

    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return

    await update.message.reply_text(
        f"*Connected broker:* `{broker.broker_id}`",
        parse_mode="Markdown",
    )


# ── Scanner shortcuts ─────────────────────────────────────────────────────────

def _scan_shortcut(scan_type: str):
    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.args = [scan_type]
        await cmd_scan(update, context)
    return _handler

cmd_pm  = _scan_shortcut("pm")
cmd_pre = _scan_shortcut("pre")
cmd_vol = _scan_shortcut("vol")
cmd_sp  = _scan_shortcut("spikes")
cmd_nk  = _scan_shortcut("parabolic")


# ── Trade commands (legacy single-broker) ─────────────────────────────────────

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/b SYMBOL SIZE [@TRIGGER] [stop|limit]`\n"
            "SIZE: N shares | N% | all | eN euros | $N USD",
            parse_mode="Markdown",
        )
        return
    symbol        = args[0].upper()
    size_token    = args[1]
    trigger_token = next((a for a in args[2:] if a.startswith("@")), None)
    forced_type   = next((a.lower() for a in args[2:] if a.lower() in ("stop", "limit")), None)
    try:
        svc   = CommandService(broker)
        order = await svc.buy(symbol, size_token, trigger_token, forced_type)
        await update.message.reply_text(
            f"✅ BUY submitted: `{order.quantity} {symbol}`\nOrder ID: `{order.broker_order_id}`",
            parse_mode="Markdown",
        )
    except CommandError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/s SYMBOL [SIZE] [@TRIGGER] [stop|limit]`\n"
            "SIZE: N shares | N% | all (default: all)",
            parse_mode="Markdown",
        )
        return
    symbol        = args[0].upper()
    size_token    = args[1] if len(args) > 1 and not args[1].startswith("@") else "all"
    trigger_token = next((a for a in args[1:] if a.startswith("@")), None)
    forced_type   = next((a.lower() for a in args[1:] if a.lower() in ("stop", "limit")), None)
    try:
        svc   = CommandService(broker)
        order = await svc.sell(symbol, size_token, trigger_token, forced_type)
        await update.message.reply_text(
            f"✅ SELL submitted: `{order.quantity} {symbol}`\nOrder ID: `{order.broker_order_id}`",
            parse_mode="Markdown",
        )
    except CommandError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/c SYMBOL`", parse_mode="Markdown")
        return
    symbol = args[0].upper()
    try:
        svc = CommandService(broker)
        res = await svc.close(symbol)
        if res.success:
            await update.message.reply_text(f"✅ Close order sent for `{symbol}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Close failed for `{symbol}`: {res.error}", parse_mode="Markdown")
    except CommandError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    try:
        svc     = CommandService(broker)
        results = await svc.closeall()
        if not results:
            await update.message.reply_text("📭 No open positions to close.")
            return
        lines = [f"{'✅' if r.success else '❌'} {r.symbol}" + (f": {r.error}" if r.error else "")
                 for r in results]
        await update.message.reply_text("*Close All*\n" + "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_stop_loss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/sl SYMBOL PRICE`", parse_mode="Markdown")
        return
    symbol = args[0].upper()
    try:
        price = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid price.")
        return
    try:
        svc = CommandService(broker)
        await svc.stop_loss(symbol, price)
        await update.message.reply_text(
            f"✅ Stop-loss updated: `{symbol}` @ `{price:,.4f}`", parse_mode="Markdown"
        )
    except CommandError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_ind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/ind SYMBOL [TF] [ext]`\nTF: 1m 5m 15m 30m 1h 4h 1d (default 1d)\nAdd `ext` for pre/after-market bars",
            parse_mode="Markdown",
        )
        return
    symbol   = args[0].upper()
    tf       = args[1].lower() if len(args) > 1 else "1d"
    extended = len(args) > 2 and args[2].lower() == "ext"
    ext_tag  = " +ext" if extended else ""
    broker   = context.bot_data.get("broker")
    await update.message.reply_text(
        f"⏳ Calculating indicators for `{symbol}` ({tf}{ext_tag})…", parse_mode="Markdown"
    )
    try:
        svc    = CommandService(broker)
        result = await svc.indicators(symbol, tf, extended=extended)
        from interfaces.telegram.formatters import v2_indicators
        await update.message.reply_text(v2_indicators(result, tf_label=f"{tf}{ext_tag}"), parse_mode="MarkdownV2")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_indp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    args = context.args or []
    svc  = CommandService(broker)

    if args and args[0].lower() == "ignore" and len(args) >= 2:
        name = " ".join(args[1:]).lower()
        svc.ignore_list_add(name)
        await update.message.reply_text(f"✅ Added `{name}` to ignore list", parse_mode="Markdown")
        return
    if args and args[0].lower() == "unignore" and len(args) >= 2:
        name = " ".join(args[1:]).lower()
        svc.ignore_list_remove(name)
        await update.message.reply_text(f"✅ Removed `{name}` from ignore list", parse_mode="Markdown")
        return
    if args and args[0].lower() == "list":
        ig   = svc.ignore_list_get()
        body = "\n".join(f"  • {e}" for e in sorted(ig)) if ig else "_(empty)_"
        await update.message.reply_text(f"*Ignore list:*\n{body}", parse_mode="Markdown")
        return

    tf = args[0].lower() if args else "1m"
    await update.message.reply_text(
        f"⏳ Running portfolio indicators ({tf} +ext)…", parse_mode="Markdown"
    )
    try:
        pr = await svc.portfolio_indicators(tf=tf, extended=True)
        if not pr.rows and not pr.skipped:
            await update.message.reply_text("📭 No open positions.", parse_mode="Markdown")
            return
        from interfaces.telegram.formatters import v2_portfolio_indicators
        await update.message.reply_text(v2_portfolio_indicators(pr, tf), parse_mode="MarkdownV2")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/q SYMBOL`", parse_mode="Markdown")
        return
    symbol = args[0].upper()
    try:
        quote = await broker.get_quote(symbol)
        spread_pct = float(quote.spread / quote.ask * 100) if quote.ask else 0.0
        ts = quote.timestamp.strftime("%H:%M:%S") if quote.timestamp else "—"
        await update.message.reply_text(
            f"📈 *Quote — {symbol}*\n"
            f"Bid:    `{float(quote.bid):,.4f}` ({quote.bid_size})\n"
            f"Ask:    `{float(quote.ask):,.4f}` ({quote.ask_size})\n"
            f"Last:   `{float(quote.last):,.4f}`\n"
            f"Mid:    `{float(quote.mid):,.4f}`\n"
            f"Spread: `{float(quote.spread):,.4f}` ({spread_pct:.3f}%)\n"
            f"Time:   `{ts}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    try:
        positions = await broker.get_positions()
        if not positions:
            await update.message.reply_text("📭 No open positions.")
            return
        total_upl  = sum(p.unrealized_pnl or 0 for p in positions)
        total_val  = sum(p.market_value   or 0 for p in positions)
        sign       = "+" if total_upl >= 0 else ""
        emoji      = "🟢" if total_upl >= 0 else "🔴"
        await update.message.reply_text(
            f"{emoji} *Open P&L*\n"
            f"Unrealised: `{sign}{total_upl:,.2f} EUR`\n"
            f"Market val: `{total_val:,.2f} EUR`\n"
            f"Positions:  `{len(positions)}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/scan pm | pre | vol | spikes | parabolic`", parse_mode="Markdown"
        )
        return
    scan_type = args[0].lower()
    await update.message.reply_text(f"⏳ Running `{scan_type}` scanner…", parse_mode="Markdown")
    try:
        svc    = CommandService(broker=None)
        output = await svc.scan(scan_type)
        if len(output) > 4000:
            output = output[:4000] + "\n…(truncated)"
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
    except CommandError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
    except Exception as e:
        logger.exception("Scanner error")
        await update.message.reply_text(f"❌ Scanner error: `{e}`", parse_mode="Markdown")


async def cmd_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    try:
        from interfaces.telegram.formatters import fmt_account
        acc = await broker.get_account_info()
        await update.message.reply_text(fmt_account(acc), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return

    fmt = (context.args[0].lower() if context.args else "table")

    try:
        positions = await broker.get_positions()
        if not positions:
            await update.message.reply_text("📭 No open positions.")
            return

        if fmt == "csv":
            lines = ["Symbol,Qty,Avg,Value,Unreal,Unreal%"]
            for p in positions:
                lines.append(
                    f"{p.symbol},{p.quantity},{p.average_price:.4f},"
                    f"{p.market_value or 0:.2f},"
                    f"{p.unrealized_pnl or 0:+.2f},"
                    f"{p.unrealized_pnl_percentage or 0:+.2f}%"
                )
            total_upl = sum(p.unrealized_pnl or 0 for p in positions)
            lines.append(f"TOTAL,,,, {total_upl:+.2f},")
            await update.message.reply_text(
                f"```\n" + "\n".join(lines) + "\n```",
                parse_mode="Markdown",
            )
        else:
            from interfaces.telegram.formatters import fmt_position
            total_upl = sum(p.unrealized_pnl or 0 for p in positions)
            total_val = sum(p.market_value   or 0 for p in positions)
            sign      = "+" if total_upl >= 0 else ""
            emoji     = "🟢" if total_upl >= 0 else "🔴"
            header    = (
                f"📋 *Positions ({len(positions)})* — "
                f"{emoji} `{sign}{total_upl:,.2f}` unreal  |  val `{total_val:,.2f}`\n"
                f"{'─' * 32}\n"
            )
            body = "\n─\n".join(fmt_position(p) for p in positions)
            msg  = header + body
            if len(msg) > 4000:
                msg = msg[:4000] + "\n…(truncated)"
            await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    try:
        from interfaces.telegram.formatters import fmt_order
        orders = await broker.get_orders()
        if not orders:
            await update.message.reply_text("📭 No recent orders.")
            return
        header = f"📋 *Orders ({min(len(orders), 10)})*\n{'─' * 32}\n"
        body   = "\n─\n".join(fmt_order(o) for o in orders[:10])
        msg    = header + body
        if len(msg) > 4000:
            msg = msg[:4000] + "\n…(truncated)"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    try:
        svc = CommandService(broker)
        r   = await svc.risk()
        eq_emoji = "✅" if (not r.equity_floor or r.own_equity >= r.equity_floor) else "🚨"
        dd_emoji = "✅" if (not r.hard_max_loss or r.drawdown < r.hard_max_loss)  else "🔴"

        floor_line = (f"{eq_emoji} Own equity:  `${r.own_equity:,.2f}` (floor `${r.equity_floor:,.2f}`)\n"
                      if r.equity_floor else f"💰 Own equity:  `${r.own_equity:,.2f}`\n")
        dd_line    = (f"{dd_emoji} Drawdown:    `${r.drawdown:,.2f}` / `${r.hard_max_loss:,.2f}` ({r.dd_pct:.1f}%)\n"
                      if r.starting_equity else "")
        loan_line  = f"🏦 Loan:        `${r.loan:,.2f}`\n" if r.loan else ""

        msg = (
            f"📊 *Risk Metrics*\n\n"
            f"{floor_line}"
            f"{dd_line}"
            f"💰 Total value: `${r.total_value:,.2f}`\n"
            f"{loan_line}"
        ).rstrip()
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


# ── Fill history & P&L ───────────────────────────────────────────────────────

def _fmt_fills(fs) -> str:
    """Format a FillSummary as Markdown for Telegram."""
    rows  = []
    for o in fs.fills:
        ts    = o.filled_timestamp.strftime("%Y-%m-%d") if o.filled_timestamp else "—"
        side  = o.side.value.upper() if o.side else "?"
        rows.append(f"`{ts}  {side:<5} {float(o.quantity):>7.0f}  {float(o.price or 0):>9.4f}`")

    sign   = lambda v: "+" if v >= 0 else ""
    lines  = [f"*Fills — {fs.symbol}*  \\({len(fs.fills)} trades\\)\n`{'─'*42}`",
              "`Date        Side  Qty      Price €`",
              *rows,
              f"`{'─'*42}`"]
    if fs.buy_qty > 0:
        lines.append(f"Avg buy:  `€{fs.avg_buy:.4f}`  \\({fs.buy_qty:.0f} shares\\)")
    if fs.sell_qty > 0:
        lines.append(f"Avg sell: `€{fs.avg_sell:.4f}`  \\({fs.sell_qty:.0f} shares\\)")
    if fs.position_qty > 0:
        bid_s = f"  bid €{fs.current_price:.4f}" if fs.current_price else ""
        avg_s = f" @ avg €{fs.broker_avg:.4f}"   if fs.broker_avg   else ""
        lines.append(f"Open: *{fs.position_qty:.0f}* shares{avg_s}{bid_s}")
        lines.append(f"Unrealized: `{sign(fs.unrealized)}{fs.unrealized:.2f}`")
    else:
        lines.append("Position: *closed*")
    total = fs.realized + fs.unrealized
    lines.append(f"Realized: `{sign(fs.realized)}{fs.realized:.2f}`")
    lines.append(f"Total P&L: `{sign(total)}{total:.2f}`")
    return "\n".join(lines)


async def cmd_fills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fills SYMBOL [N] — fill history + P&L."""
    args   = context.args or []
    broker = context.bot_data.get("broker")
    if not broker:
        await update.message.reply_text("❌ No broker connected.")
        return
    if not args:
        await update.message.reply_text("Usage: `/fills SYMBOL [N]`", parse_mode="Markdown")
        return
    symbol = args[0].upper()
    limit  = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await update.message.reply_text(f"⏳ Fetching fills for `{symbol}`…", parse_mode="Markdown")
    try:
        svc = CommandService(broker)
        fs  = await svc.fills(symbol, limit)
        if not fs.fills:
            await update.message.reply_text(f"No filled orders found for `{symbol}`.", parse_mode="Markdown")
            return
        await update.message.reply_text(_fmt_fills(fs), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


# ── Level Monitor — shared helpers ────────────────────────────────────────────

_ml_monitors: dict = {}  # symbol → {task, monitor, manager, levels, filters}

_ML_FILTER_MAP = {
    "break_up":    "BREAK_ABOVE",
    "up":          "BREAK_ABOVE",
    "break_down":  "BREAK_BELOW",
    "down":        "BREAK_BELOW",
    "bounce":      "BOUNCE",
    "reject":      "REJECTION",
    "rejection":   "REJECTION",
    "false":       "FALSE_BREAK",
    "false_break": "FALSE_BREAK",
}


async def _ml_fetch_price(symbol: str, broker=None) -> float:
    """Return current price via broker quote or FmpPriceService (with fallback routing)."""
    if broker is not None:
        try:
            q = await broker.get_quote(symbol)
            price = float(q.last or q.mid or q.bid or 0)
            if price > 0:
                return price
        except Exception:
            pass
    from services.price_service import FmpPriceService
    svc    = FmpPriceService(api_key=os.environ.get("FMP_API_KEY", ""), symbols=[symbol])
    quotes = await svc.get_quotes()
    q      = quotes.get(symbol)
    return float(q.price) if q and q.price else 0.0


def _fmt_level_event(evt) -> str:
    _LABEL = {
        "break_above": "▲ BREAK ABOVE",
        "break_below": "▼ BREAK BELOW",
        "bounce":      "↑ BOUNCE",
        "rejection":   "↓ REJECTION",
        "false_break": "✗ FALSE BREAK",
    }
    label = _LABEL.get(evt.event.value, evt.event.value.upper())
    conv  = "✓" if evt.convincing else "~"
    from core.entities.level_event import LevelEvent
    is_break = evt.event in (LevelEvent.BREAK_ABOVE, LevelEvent.BREAK_BELOW)
    dwell_label = "outside" if is_break else "dwell"
    dwell = f"  {dwell_label} {evt.dwell_seconds:.0f}s" if evt.dwell_seconds else ""
    orig  = f"  orig={evt.original_break.value}" if evt.original_break else ""
    ts    = evt.timestamp.strftime("%H:%M:%S")
    return (
        f"*{label}*  `{evt.symbol}`  {ts}\n"
        f"Level: `{evt.level:.2f}`  Price: `{evt.price:.4f}`  {conv}\n"
        f"Zone: `[{evt.zone_lo:.3f}–{evt.zone_hi:.3f}]`  ATR: `{evt.atr:.4f}`{dwell}{orig}"
    )


def _ml_parse_args(args: list) -> tuple:
    """Return (symbol, levels, event_filters) or raise ValueError."""
    from core.entities.level_event import LevelEvent
    symbol: str = args[0].upper()
    levels: list = []
    filters: set = set()
    for tok in args[1:]:
        try:
            levels.append(float(tok))
        except ValueError:
            key = _ML_FILTER_MAP.get(tok.lower())
            if key is None:
                raise ValueError(
                    f"Unknown filter '{tok}'. Valid: break_up, break_down, bounce, reject, false_break"
                )
            filters.add(getattr(LevelEvent, key))
    return symbol, levels, filters


async def _ml_start_monitor(
    symbol: str,
    levels: list,
    event_filters: set,
    send_fn,
    break_fn=None,   # optional extra channel called only for BREAK_ABOVE / BREAK_BELOW
) -> tuple:
    """Spin up PriceStateManager. Returns (task, None, manager).

    NOTE: price ticks must come from the external price monitor service
    (run_live_monitor.py).  No PriceMonitor is started here.
    """
    from adapters.events.local_event_bus import LocalEventBus
    from core.entities.level_event import LevelEvent
    from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
    from services.price_state_manager import PriceStateManager

    _BREAKS = {LevelEvent.BREAK_ABOVE, LevelEvent.BREAK_BELOW}
    bus = LocalEventBus()

    async def _on_event(payload):
        text = _fmt_level_event(payload)
        if not event_filters or payload.event in event_filters:
            await send_fn(text)
        if break_fn and payload.event in _BREAKS:
            await break_fn(text)

    for evt in LevelEvent:
        bus.subscribe(evt, _on_event)

    fmp_key = os.environ.get("FMP_API_KEY", "")
    from infrastructure.cache.redis_cache import RedisCache
    from services.price_history_service import PriceHistoryService
    _fmp     = FmpDataFetcher({"api_key": fmp_key})
    _history = PriceHistoryService(
        fetcher=_fmp,
        cache=RedisCache(url=os.environ.get("REDIS_URL", "redis://localhost:6379")),
        fetcher_name="fmp",
    )
    manager = PriceStateManager(
        levels={symbol: levels},
        bus=bus,
        history=_history,
    )
    await manager.start()

    async def _run():
        try:
            await asyncio.Future()   # run until cancelled
        finally:
            await manager.stop()

    task = asyncio.create_task(_run())
    return task, None, manager


async def cmd_ml(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ml SYMBOL LEVEL [...] [filter ...] — level monitor."""
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "*Level Monitor*\n"
            "`/ml SYMBOL LEVEL [LEVEL...] [filter...]`\n"
            "Filters: `break_up`  `break_down`  `bounce`  `reject`  `false_break`\n"
            "`/ml status [SYMBOL]` —  current price + distance from levels\n"
            "`/ml stop SYMBOL`  —  stop monitoring\n"
            "`/ml list`         —  list active monitors\n"
            "`/ml save`         —  save active monitors to disk\n"
            "`/ml load`         —  load and start saved monitors\n"
            "`/ml clear`        —  delete saved monitors",
            parse_mode="Markdown",
        )
        return

    sub = args[0].lower()

    if sub == "list":
        if not _ml_monitors:
            await update.message.reply_text("No active level monitors.")
            return
        lines = []
        for sym, entry in _ml_monitors.items():
            lvl_str = ", ".join(str(l) for l in entry["levels"])
            flt_str = ", ".join(f.value for f in entry["filters"]) if entry["filters"] else "all"
            lines.append(f"• `{sym}`: [{lvl_str}]  filters=[{flt_str}]")
        await update.message.reply_text(
            "*Active Level Monitors*\n" + "\n".join(lines),
            parse_mode="Markdown",
        )
        return

    if sub == "save":
        data = {
            sym: {"levels": entry["levels"], "filters": [f.value for f in entry["filters"]]}
            for sym, entry in _ml_monitors.items()
        }
        _save_ml_levels(data)
        await update.message.reply_text(
            f"✅ Saved {len(data)} monitor(s) to disk", parse_mode="Markdown"
        )
        return

    if sub == "clear":
        _save_ml_levels({})
        await update.message.reply_text("✅ Saved levels cleared", parse_mode="Markdown")
        return

    if sub == "load":
        from core.entities.level_event import LevelEvent as _LE
        saved = _load_ml_levels()
        if not saved:
            await update.message.reply_text("No saved levels found.", parse_mode="Markdown")
            return
        chat_id = update.effective_chat.id
        bot     = context.bot
        async def _load_send_fn(text: str) -> None:
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as exc:
                logger.warning("ml send_message failed: %s", exc)
        started = []
        for sym, cfg in saved.items():
            if sym in _ml_monitors:
                continue
            lvls = [float(v) for v in cfg.get("levels", [])]
            filt = set()
            for v in cfg.get("filters", []):
                try:
                    filt.add(_LE(v))
                except ValueError:
                    pass
            try:
                task, monitor, manager = await _ml_start_monitor(sym, lvls, filt, _load_send_fn)
            except Exception as e:
                logger.warning("ml load failed for %s: %s", sym, e)
                continue
            _ml_monitors[sym] = {"task": task, "monitor": monitor, "manager": manager,
                                  "levels": lvls, "filters": list(filt)}
            started.append(sym)
        if started:
            syms = ", ".join(f"`{s}`" for s in started)
            await update.message.reply_text(f"✅ Loaded: {syms}", parse_mode="Markdown")
        else:
            await update.message.reply_text("Nothing new to load.", parse_mode="Markdown")
        return

    if sub == "status":
        target   = args[1].upper() if len(args) > 1 else None
        snapshot = {s: e for s, e in _ml_monitors.items() if target is None or s == target}
        if not snapshot:
            msg = f"No monitor for `{target}`" if target else "No active level monitors."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return
        broker = context.bot_data.get("broker")
        prices = {}
        for sym in snapshot:
            try:
                prices[sym] = await _ml_fetch_price(sym, broker)
            except Exception:
                prices[sym] = 0.0
        from interfaces.telegram.formatters import fmt_ml_status
        await update.message.reply_text(fmt_ml_status(snapshot, prices), parse_mode="Markdown")
        return

    if sub == "stop":
        if len(args) < 2:
            await update.message.reply_text("Usage: `/ml stop SYMBOL`", parse_mode="Markdown")
            return
        symbol = args[1].upper()
        entry  = _ml_monitors.pop(symbol, None)
        if not entry:
            await update.message.reply_text(f"No monitor for `{symbol}`", parse_mode="Markdown")
            return
        entry["task"].cancel()
        await update.message.reply_text(
            f"✅ Stopped monitor for `{symbol}`", parse_mode="Markdown"
        )
        return

    try:
        symbol, levels, event_filters = _ml_parse_args(args)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
        return

    if not levels:
        await update.message.reply_text(
            "❌ No levels given.\nUsage: `/ml SYMBOL LEVEL [LEVEL...]`",
            parse_mode="Markdown",
        )
        return

    if symbol in _ml_monitors:
        await update.message.reply_text(
            f"Already monitoring `{symbol}`. Use `/ml stop {symbol}` first.",
            parse_mode="Markdown",
        )
        return

    chat_id = update.effective_chat.id
    bot     = context.bot

    async def send_fn(text: str) -> None:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("ml send_message failed: %s", exc)

    await update.message.reply_text(
        f"⏳ Loading indicators for `{symbol}`…", parse_mode="Markdown"
    )
    try:
        task, monitor, manager = await _ml_start_monitor(symbol, levels, event_filters, send_fn)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to start: `{e}`", parse_mode="Markdown")
        return

    _ml_monitors[symbol] = {
        "task":    task,
        "monitor": monitor,
        "manager": manager,
        "levels":  levels,
        "filters": list(event_filters),
    }

    flt_str = ", ".join(f.value for f in event_filters) if event_filters else "all events"
    lvl_str = ", ".join(str(l) for l in levels)
    await update.message.reply_text(
        f"✅ Monitoring `{symbol}` at [{lvl_str}]\nFilters: {flt_str}",
        parse_mode="Markdown",
    )


# ── CommandHandler — multi-broker session-aware handler class ─────────────────

from typing import Any, Dict, Optional as _Optional

from core.utils.log_helper import LK as _LK, set_log_context as _set_ctx
from interfaces.telegram.arg_parser import parse as _parse, require_args as _require_args
from interfaces.telegram.session import SessionManager
from interfaces.telegram import formatters as _fmt

_log = _lh_getLogger(__name__, app_name="tg-bot")


class CommandHandler:
    """
    Multi-broker, session-aware Telegram command handler.

    Wires /commands to broker actions + data reads. Resolves the target
    broker at runtime from session context + optional --broker flag.

    Parameters
    ----------
    brokers:
        Registry of all available broker instances, e.g.::

            {
                "capital_live": CapitalBroker(...),
                "capital_demo": CapitalBroker(..., is_demo=True),
                "ibkr_live":    IBKRBroker(...),
                "ibkr_demo":    IBKRBroker(..., is_demo=True),
                "etoro":        eToroBroker(...),
            }

    sessions:
        SessionManager that tracks active_broker + active_account per chat.

    risk_monitor:
        Optional object with .daily_pnl, .daily_trades, .daily_wins,
        .daily_losses, .win_rate, ._daily_loss_limit. Required for /pnl and /risk.

    compound_tracker:
        Optional object with equity tracking attributes. Required for /progress.

    strategies:
        Optional {strategy_id: strategy_instance} for /halt and /resume.

    config_default_broker:
        Fallback broker name when session has none set.
    """

    def __init__(
        self,
        brokers:               Dict[str, Any],
        sessions:              SessionManager,
        risk_monitor:          _Optional[Any]           = None,
        compound_tracker:      _Optional[Any]           = None,
        strategies:            _Optional[Dict[str, Any]] = None,
        config_default_broker: str = "capital",
    ) -> None:
        self._brokers          = brokers
        self._sessions         = sessions
        self._risk_monitor     = risk_monitor
        self._compound_tracker = compound_tracker
        self._strategies       = strategies or {}
        self._default_broker   = config_default_broker
        self._level_monitors: dict = {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_broker(
        self,
        chat_id:      int,
        flag_broker:  _Optional[str],
        flag_account: _Optional[str],
    ) -> tuple:
        name, account = self._sessions.resolve(
            chat_id,
            flag_broker=flag_broker,
            flag_account=flag_account,
            config_default=self._default_broker,
        )
        return self._brokers.get(name), name, account

    async def _reply(self, update: Update, text: str) -> None:
        await update.message.reply_text(text, parse_mode="MarkdownV2")

    async def _broker_or_error(
        self,
        update:       Update,
        chat_id:      int,
        flag_broker:  _Optional[str],
        flag_account: _Optional[str],
    ) -> tuple:
        broker, name, account = self._resolve_broker(chat_id, flag_broker, flag_account)
        # Stamp broker into log context so every log line in this command shows [brk:name]
        _set_ctx({_LK.BROKER: name})
        if broker is None:
            available = ", ".join(self._brokers.keys()) or "none"
            await self._reply(
                update,
                _fmt.v2_error(f"Broker '{name}' not found. Available: {available}"),
            )
        return broker, name, account

    # ── Context commands ──────────────────────────────────────────────────────

    async def use(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/use [broker] [account]  — set active broker and account."""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)

        if not parsed.positional:
            session = self._sessions.get(chat_id)
            await self._reply(update, f"*Current context*\n{_fmt._esc(session.describe())}")
            return

        broker  = parsed.positional[0].lower()
        account = parsed.positional[1] if len(parsed.positional) > 1 else ""

        if broker not in self._brokers:
            available = ", ".join(self._brokers.keys()) or "none"
            await self._reply(
                update,
                _fmt.v2_error(f"Unknown broker '{broker}'. Available: {available}"),
            )
            return

        self._sessions.get(chat_id).set(broker, account)
        await self._reply(update, _fmt.v2_context_set(broker, account))

    async def context(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/context — show current broker + account."""
        session = self._sessions.get(update.effective_chat.id)
        await self._reply(update, f"*Context*\n{_fmt._esc(session.describe())}")

    # ── Read commands ─────────────────────────────────────────────────────────

    async def status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/status [--broker B] [--account A]"""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)
        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return
        try:
            info = await broker.get_account_info()
            await self._reply(update, _fmt.v2_account_status(info, name, account))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/positions [SYMBOL] [--broker B]"""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)
        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return
        try:
            symbol = parsed.positional[0].upper() if parsed.positional else None
            if symbol:
                p = await _find_position(broker, symbol)
                pos = [p] if p else []
            else:
                pos = await broker.get_positions()
            await self._reply(update, _fmt.v2_positions_list(pos, name, account))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def orders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/orders [--broker B]"""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)
        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return
        try:
            all_orders = await broker.get_orders()
            await self._reply(update, _fmt.v2_orders_list(all_orders, name))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def quote(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/quote SYMBOL [--broker B] — current bid, ask, last."""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)

        err = _require_args(parsed, 1, "/quote SYMBOL")
        if err:
            await self._reply(update, err)
            return

        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return

        symbol = parsed.positional[0].upper()
        _set_ctx({_LK.SYMBOL: symbol})
        try:
            quote = await broker.get_quote(symbol)
            await self._reply(update, _fmt.v2_quote(quote))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def ind(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/ind SYMBOL [TF] — ATR, RSI, EMA 8/20, SuperTrend(10,2)."""
        parsed = _parse(update.message.text)
        err    = _require_args(parsed, 1, "/ind SYMBOL [TF]")
        if err:
            await self._reply(update, err)
            return
        symbol   = parsed.positional[0].upper()
        tf       = parsed.positional[1].lower() if len(parsed.positional) > 1 else "1d"
        extended = len(parsed.positional) > 2 and parsed.positional[2].lower() == "ext"
        ext_tag  = " +ext" if extended else ""
        _set_ctx({_LK.SYMBOL: symbol})
        await update.message.reply_text(
            f"⏳ Calculating indicators for `{symbol}` \\({_fmt._esc(tf + ext_tag)}\\)…",
            parse_mode="MarkdownV2",
        )
        try:
            data, ts = await _run_indicators(symbol, tf, extended=extended)
            await self._reply(update, _fmt.v2_indicators(symbol, f"{tf}{ext_tag}", ts, data))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def indp(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/indp [TF] | ignore NAME | unignore NAME | list"""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)
        args    = parsed.positional

        if args and args[0].lower() == "ignore" and len(args) >= 2:
            name = " ".join(args[1:]).lower()
            ig = _load_ignore(); ig.add(name); _save_ignore(ig)
            await self._reply(update, _fmt.v2_success(f"Added '{name}' to ignore list"))
            return
        if args and args[0].lower() == "unignore" and len(args) >= 2:
            name = " ".join(args[1:]).lower()
            ig = _load_ignore(); ig.discard(name); _save_ignore(ig)
            await self._reply(update, _fmt.v2_success(f"Removed '{name}' from ignore list"))
            return
        if args and args[0].lower() == "list":
            ig = _load_ignore()
            if ig:
                body = _fmt._esc("\n".join(f"  • {e}" for e in sorted(ig)))
                await self._reply(update, f"*Ignore list:*\n{body}")
            else:
                await self._reply(update, _fmt.v2_success("Ignore list is empty"))
            return

        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return

        tf = args[0].lower() if args else "1m"
        await update.message.reply_text(
            f"⏳ Running portfolio indicators \\({_fmt._esc(tf)} \\+ext\\)…",
            parse_mode="MarkdownV2",
        )
        try:
            results, skipped = await _run_portfolio_indicators(broker, tf=tf, extended=True)
            if not results and not skipped:
                await self._reply(update, "📭 No open positions\\.")
                return
            await self._reply(update, _fmt.v2_portfolio_indicators(tf, results, skipped))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/pnl — today's realised PnL from RiskMonitor."""
        if self._risk_monitor is None:
            await self._reply(update, _fmt.v2_error("RiskMonitor not configured"))
            return
        rm   = self._risk_monitor
        sign = "+" if rm.daily_pnl >= 0 else ""
        msg  = (
            f"*📅 Today's PnL*\n"
            f"Realised: `{_fmt._esc(f'{sign}{rm.daily_pnl:,.2f}')}`\n"
            f"Trades:   `{_fmt._esc(str(rm.daily_trades))}` "
            f"\\(W: {_fmt._esc(str(rm.daily_wins))} / L: {_fmt._esc(str(rm.daily_losses))}\\)\n"
            f"Win rate: `{_fmt._esc(f'{rm.win_rate*100:.1f}')}%`"
        )
        await self._reply(update, msg)

    async def progress(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/progress — compound tracker vs daily target."""
        if self._compound_tracker is None:
            await self._reply(update, _fmt.v2_error("CompoundTracker not configured"))
            return
        ct             = self._compound_tracker
        current        = ct.current_equity
        starting       = ct.starting_equity
        target         = ct.target_equity
        daily_pct      = ct.daily_target_pct * 100
        days_done      = ct.days_completed
        eff_days       = ct.effective_days
        days_remaining = max(0, eff_days - days_done)

        pct_done     = current / target * 100 if target else 0
        total_return = (current - starting) / starting * 100 if starting else 0
        implied      = ((target / current) ** (1 / days_remaining) - 1) * 100 if days_remaining > 0 and current > 0 else 0

        bar_filled = int(pct_done / 10)
        bar        = "█" * bar_filled + "░" * (10 - bar_filled)

        msg = (
            f"*📈 Compound Progress*\n"
            f"`{_fmt._esc(bar)}` {_fmt._esc(f'{pct_done:.1f}')}%\n\n"
            f"Equity:  `{_fmt._esc(f'${current:,.2f}')}`\n"
            f"Target:  `{_fmt._esc(f'${target:,.0f}')}`\n"
            f"Return:  `{_fmt._esc(f'{total_return:+.2f}%')}`\n\n"
            f"Day PnL: {_fmt._pnl_v2(ct.session_pnl)}\n"
            f"Days:    `{_fmt._esc(str(days_done))}/{_fmt._esc(str(eff_days))}` "
            f"\\({_fmt._esc(str(days_remaining))} remaining\\)\n"
            f"Needed:  `{_fmt._esc(f'{implied:.4f}%')}/day` to hit target"
        )
        await self._reply(update, msg)

    async def risk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/risk — risk limit status."""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)
        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return

        rm = self._risk_monitor
        if rm is None:
            await self._reply(update, _fmt.v2_error("RiskMonitor not configured"))
            return

        try:
            info         = await broker.get_account_info()
            loan_amount  = getattr(broker.config, "loan_amount",     50_000.0)
            equity_floor = getattr(broker.config, "equity_floor",    55_000.0)
            hard_max     = getattr(broker.config, "hard_max_loss",   20_000.0)
            start_equity = getattr(broker.config, "starting_equity", 122_562.0)
            own_equity   = (info.current_value or 0) - loan_amount

            await self._reply(update, _fmt.v2_risk_status(
                daily_pnl=rm.daily_pnl,
                daily_loss_limit=rm._daily_loss_limit,
                equity_floor=equity_floor,
                own_equity=own_equity,
                hard_max_loss=hard_max,
                starting_equity=start_equity,
                current_equity=info.current_value or 0,
            ))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    # ── Trade commands ────────────────────────────────────────────────────────

    async def buy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/buy SYMBOL QTY [--broker B] [--account A]"""
        await self._place_order(update, side="BUY")

    async def sell(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/sell SYMBOL QTY [--broker B] [--account A]"""
        await self._place_order(update, side="SELL")

    async def _place_order(self, update: Update, side: str) -> None:
        from core.entities.broker_entities import OrderSide, OrderType
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)

        err = _require_args(parsed, 2, f"/{side.lower()} SYMBOL QTY")
        if err:
            await self._reply(update, err)
            return

        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return

        try:
            symbol = parsed.positional[0].upper()
            qty    = float(parsed.positional[1])
        except (IndexError, ValueError) as e:
            await self._reply(update, _fmt.v2_error(f"Invalid arguments: {e}"))
            return

        _set_ctx({_LK.SYMBOL: symbol})
        try:
            order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
            order = await broker.place_order(
                symbol=symbol,
                quantity=qty,
                side=order_side,
                order_type=OrderType.MARKET,
            )
            await self._reply(update, _fmt.v2_order_placed(order, name))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/close SYMBOL [--broker B] [--account A]"""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)

        err = _require_args(parsed, 1, "/close SYMBOL")
        if err:
            await self._reply(update, err)
            return

        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return

        symbol = parsed.positional[0].upper()
        _set_ctx({_LK.SYMBOL: symbol})
        try:
            position = await _find_position(broker, symbol)
            if not position:
                await self._reply(update, _fmt.v2_error(f"No open position for {symbol}"))
                return
            ok = await broker.close_position(position.id)
            if ok:
                await self._reply(update, _fmt.v2_success(f"Close order sent for {symbol}"))
            else:
                await self._reply(update, _fmt.v2_error(f"Close failed for {symbol}"))
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def closeall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/closeall [--broker B] — close all open positions."""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)
        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return

        try:
            positions = await broker.get_positions()
            if not positions:
                await self._reply(update, _fmt.v2_success("No open positions to close"))
                return
            results = []
            for pos in positions:
                try:
                    await broker.close_position(pos.id)
                    results.append(f"✅ {pos.symbol}")
                except Exception as e:
                    results.append(f"❌ {pos.symbol}: {e}")
            await self._reply(
                update,
                f"*Close All*\n{_fmt._esc(chr(10).join(results))}",
            )
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    async def stop_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/stop SYMBOL PRICE [--broker B] — update stop loss."""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)

        err = _require_args(parsed, 2, "/stop SYMBOL PRICE")
        if err:
            await self._reply(update, err)
            return

        broker, name, account = await self._broker_or_error(
            update, chat_id, parsed.broker, parsed.account
        )
        if broker is None:
            return

        try:
            symbol     = parsed.positional[0].upper()
            stop_price = float(parsed.positional[1])
        except (IndexError, ValueError) as e:
            await self._reply(update, _fmt.v2_error(f"Invalid arguments: {e}"))
            return

        _set_ctx({_LK.SYMBOL: symbol})
        try:
            position = await _find_position(broker, symbol)
            if not position:
                await self._reply(update, _fmt.v2_error(f"No open position for {symbol}"))
                return
            ok = await broker.update_position_stops(
                position.id, stop_loss_price=stop_price
            )
            if ok:
                await self._reply(
                    update, _fmt.v2_success(f"Stop updated for {symbol} @ {stop_price:,.4f}")
                )
            else:
                await self._reply(
                    update, _fmt.v2_error(f"Stop update not supported by broker '{name}'")
                )
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    # ── Strategy control ──────────────────────────────────────────────────────

    async def halt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/halt [SYMBOL | strategy_id] — halt one or all strategies."""
        parsed = _parse(update.message.text)
        target = parsed.positional[0].upper() if parsed.positional else None
        halted = []

        for sid, strategy in self._strategies.items():
            if target and target not in (sid.upper(), getattr(strategy, "_symbol", "").upper()):
                continue
            if hasattr(strategy, "halt"):
                strategy.halt()
                halted.append(sid)

        if halted:
            await self._reply(update, _fmt.v2_success(f"Halted: {', '.join(halted)}"))
        else:
            msg = f"No strategy found for '{target}'" if target else "No strategies registered"
            await self._reply(update, _fmt.v2_error(msg))

    async def resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/resume [SYMBOL | strategy_id] — resume one or all strategies."""
        parsed  = _parse(update.message.text)
        target  = parsed.positional[0].upper() if parsed.positional else None
        resumed = []

        for sid, strategy in self._strategies.items():
            if target and target not in (sid.upper(), getattr(strategy, "_symbol", "").upper()):
                continue
            if hasattr(strategy, "resume"):
                strategy.resume()
                resumed.append(sid)

        if resumed:
            await self._reply(update, _fmt.v2_success(f"Resumed: {', '.join(resumed)}"))
        else:
            msg = f"No strategy found for '{target}'" if target else "No strategies registered"
            await self._reply(update, _fmt.v2_error(msg))

    # ── Help ──────────────────────────────────────────────────────────────────

    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/help — show all commands."""
        await self._reply(update, _fmt.v2_help_text(list(self._brokers.keys())))

    # ── Fill history & P&L ───────────────────────────────────────────────────

    async def fills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/fills SYMBOL [N] — fill history + realized/unrealized P&L."""
        chat_id = update.effective_chat.id
        parsed  = _parse(update.message.text)

        err = _require_args(parsed, 1, "/fills SYMBOL [N]")
        if err:
            await self._reply(update, err)
            return

        symbol = parsed.positional[0].upper()
        limit  = None
        if len(parsed.positional) > 1 and parsed.positional[1].isdigit():
            limit = int(parsed.positional[1])

        broker, _, _ = await self._broker_or_error(update, chat_id, parsed.broker, parsed.account)
        if broker is None:
            return

        await update.message.reply_text(f"⏳ Fetching fills for `{symbol}`…", parse_mode="Markdown")
        try:
            fills = await _get_fills(broker, symbol)
            if not fills:
                await self._reply(update, _fmt.v2_error(f"No filled orders found for {symbol}"))
                return
            if limit:
                fills = fills[-limit:]

            open_pos      = await _get_position_for_ticker(broker, symbol)
            position_size = float(open_pos.quantity)      if open_pos else 0.0
            broker_avg    = float(open_pos.average_price) if open_pos else 0.0
            current_bid   = 0.0
            try:
                q           = await broker.get_quote(symbol)
                current_bid = float(q.bid or q.mid or q.last or 0)
            except Exception:
                pass

            result = _calc_pnl(fills, position_size, current_bid)

            if position_size > 0 and broker_avg > 0 and current_bid > 0:
                unrealized = (current_bid - broker_avg) * position_size
            else:
                unrealized = result["unrealized"]
                broker_avg = result["avg_buy"]

            await update.message.reply_text(
                _fmt_fills(symbol, result, len(fills), position_size, broker_avg, current_bid, unrealized),
                parse_mode="Markdown",
            )
        except Exception as e:
            await self._reply(update, _fmt.v2_error(str(e)))

    # ── Level monitor ─────────────────────────────────────────────────────────

    async def monitor_levels(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/ml SYMBOL LEVEL [...] [filter ...] — start or manage a level monitor."""
        args    = ctx.args or []
        chat_id = update.effective_chat.id

        if not args:
            await update.message.reply_text(
                "*Level Monitor*\n"
                "`/ml SYMBOL LEVEL [LEVEL...] [filter...]`\n"
                "Filters: `break_up`  `break_down`  `bounce`  `reject`  `false_break`\n"
                "`/ml stop SYMBOL`  —  stop monitoring\n"
                "`/ml list`         —  list active monitors\n"
                "`/ml save`         —  save active monitors to disk\n"
                "`/ml load`         —  load and start saved monitors\n"
                "`/ml clear`        —  delete saved monitors",
                parse_mode="Markdown",
            )
            return

        sub = args[0].lower()

        if sub == "list":
            if not self._level_monitors:
                await update.message.reply_text("No active level monitors.")
                return
            lines = []
            for sym, entry in self._level_monitors.items():
                lvl_str = ", ".join(str(l) for l in entry["levels"])
                flt_str = ", ".join(f.value for f in entry["filters"]) if entry["filters"] else "all"
                lines.append(f"• `{sym}`: [{lvl_str}]  filters=[{flt_str}]")
            await update.message.reply_text(
                "*Active Level Monitors*\n" + "\n".join(lines),
                parse_mode="Markdown",
            )
            return

        if sub == "save":
            data = {
                sym: {"levels": entry["levels"], "filters": [f.value for f in entry["filters"]]}
                for sym, entry in self._level_monitors.items()
            }
            _save_ml_levels(data)
            await update.message.reply_text(
                f"✅ Saved {len(data)} monitor(s) to disk", parse_mode="Markdown"
            )
            return

        if sub == "clear":
            _save_ml_levels({})
            await update.message.reply_text("✅ Saved levels cleared", parse_mode="Markdown")
            return

        if sub == "load":
            from core.entities.level_event import LevelEvent as _LE
            saved = _load_ml_levels()
            if not saved:
                await update.message.reply_text("No saved levels found.", parse_mode="Markdown")
                return
            bot = ctx.bot
            async def _load_send_fn(text: str) -> None:
                try:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                except Exception as exc:
                    logger.warning("ml send_message failed: %s", exc)
            started = []
            for sym, cfg in saved.items():
                if sym in self._level_monitors:
                    continue
                lvls = [float(v) for v in cfg.get("levels", [])]
                filt = set()
                for v in cfg.get("filters", []):
                    try:
                        filt.add(_LE(v))
                    except ValueError:
                        pass
                try:
                    task, monitor, manager = await _ml_start_monitor(sym, lvls, filt, _load_send_fn)
                except Exception as e:
                    logger.warning("ml load failed for %s: %s", sym, e)
                    continue
                self._level_monitors[sym] = {"task": task, "monitor": monitor, "manager": manager,
                                              "levels": lvls, "filters": list(filt)}
                started.append(sym)
            if started:
                syms = ", ".join(f"`{s}`" for s in started)
                await update.message.reply_text(f"✅ Loaded: {syms}", parse_mode="Markdown")
            else:
                await update.message.reply_text("Nothing new to load.", parse_mode="Markdown")
            return

        if sub == "status":
            target   = args[1].upper() if len(args) > 1 else None
            snapshot = {s: e for s, e in self._level_monitors.items() if target is None or s == target}
            if not snapshot:
                msg = f"No monitor for `{target}`" if target else "No active level monitors."
                await update.message.reply_text(msg, parse_mode="Markdown")
                return
            parsed  = _parse(update.message.text)
            broker, _, _ = self._resolve_broker(chat_id, parsed.broker, parsed.account)
            prices = {}
            for sym in snapshot:
                try:
                    prices[sym] = await _ml_fetch_price(sym, broker)
                except Exception:
                    prices[sym] = 0.0
            from interfaces.telegram.formatters import fmt_ml_status
            await update.message.reply_text(fmt_ml_status(snapshot, prices), parse_mode="Markdown")
            return

        if sub == "stop":
            if len(args) < 2:
                await update.message.reply_text("Usage: `/ml stop SYMBOL`", parse_mode="Markdown")
                return
            symbol = args[1].upper()
            entry  = self._level_monitors.pop(symbol, None)
            if not entry:
                await update.message.reply_text(f"No monitor for `{symbol}`", parse_mode="Markdown")
                return
            entry["task"].cancel()
            await update.message.reply_text(
                f"✅ Stopped monitor for `{symbol}`", parse_mode="Markdown"
            )
            return

        try:
            symbol, levels, event_filters = _ml_parse_args(args)
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
            return

        if not levels:
            await update.message.reply_text(
                "❌ No levels given.\nUsage: `/ml SYMBOL LEVEL [LEVEL...]`",
                parse_mode="Markdown",
            )
            return

        if symbol in self._level_monitors:
            await update.message.reply_text(
                f"Already monitoring `{symbol}`. Use `/ml stop {symbol}` first.",
                parse_mode="Markdown",
            )
            return

        bot = ctx.bot

        async def send_fn(text: str) -> None:
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as exc:
                logger.warning("ml send_message failed: %s", exc)

        await update.message.reply_text(
            f"⏳ Loading indicators for `{symbol}`…", parse_mode="Markdown"
        )
        try:
            task, monitor, manager = await _ml_start_monitor(
                symbol, levels, event_filters, send_fn
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to start: `{e}`", parse_mode="Markdown")
            return

        self._level_monitors[symbol] = {
            "task":    task,
            "monitor": monitor,
            "manager": manager,
            "levels":  levels,
            "filters": list(event_filters),
        }

        flt_str = ", ".join(f.value for f in event_filters) if event_filters else "all events"
        lvl_str = ", ".join(str(l) for l in levels)
        await update.message.reply_text(
            f"✅ Monitoring `{symbol}` at [{lvl_str}]\nFilters: {flt_str}",
            parse_mode="Markdown",
        )
