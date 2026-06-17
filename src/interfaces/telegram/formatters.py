"""
interfaces.telegram.formatters

Convert domain entities and scanner output into clean Telegram messages.
All messages use Markdown (parse_mode="Markdown").
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from core.entities.broker_entities import AccountInfo, Order, OrderStatus
from core.entities.position_types import Position


def fmt_order(order: Order) -> str:
    status_emoji = {
        OrderStatus.FILLED:           "✅",
        OrderStatus.REJECTED:         "❌",
        OrderStatus.CANCELLED:        "🚫",
        OrderStatus.PARTIALLY_FILLED: "⚡",
        OrderStatus.SUBMITTED:        "📤",
        OrderStatus.PENDING:          "⏳",
    }.get(order.status, "❓")

    lines = [
        f"{status_emoji} *ORDER {order.status.value.upper()}*",
        f"Symbol:  `{order.symbol}`",
        f"Side:    `{order.side.value.upper()}`",
        f"Qty:     `{order.quantity}`",
        f"Price:   `${order.average_fill_price:.2f}`" if order.average_fill_price else "",
        f"Fees:    `${order.fees:.2f}`" if order.fees else "",
        f"ID:      `{order.id}`",
    ]
    if order.enter_reason:
        lines.append(f"Reason:  _{order.enter_reason}_")
    if order.reject_reason:
        lines.append(f"Reject:  _{order.reject_reason}_")

    return "\n".join(l for l in lines if l)


def fmt_position(pos: Position) -> str:
    pnl_emoji = "📈" if pos.unrealized_pnl >= 0 else "📉"
    return (
        f"{pnl_emoji} *{pos.symbol}* ({pos.side.value.upper()})\n"
        f"Qty:      `{pos.quantity}`\n"
        f"Avg:      `${pos.average_price:.2f}`\n"
        f"Value:    `${pos.market_value:.2f}`\n"
        f"Unreal:   `${pos.unrealized_pnl:+.2f}` ({pos.unrealized_pnl_percentage:+.2f}%)\n"
        f"SL:       `${pos.stop_loss_price:.2f}`\n"
        f"TP:       `${pos.take_profit_price:.2f}`"
    )


def fmt_account(acc: AccountInfo) -> str:
    return (
        f"💼 *Account — {acc.account_name}*\n"
        f"Value:     `${acc.current_value:,.2f}`\n"
        f"Cash:      `${acc.cash_in_hand:,.2f}`\n"
        f"Margin:    `${acc.margin_used:,.2f}` used / `${acc.margin_available:,.2f}` free\n"
        f"Leverage:  `{acc.leverage}x`\n"
        f"Currency:  `{acc.currency}`"
    )


def fmt_risk_alert(event_name: str, data: dict) -> str:
    if event_name == "equity_floor_hit":
        return (
            f"🚨 *EQUITY FLOOR HIT*\n"
            f"Own equity: `${data.get('own_equity', 0):,.2f}`\n"
            f"Floor:      `${data.get('equity_floor', 0):,.2f}`\n"
            f"Loan:       `${data.get('loan_amount', 0):,.2f}`"
        )
    if event_name == "daily_loss_limit":
        return (
            f"🔴 *DAILY LOSS LIMIT HIT — GO TO CASH*\n"
            f"Drawdown:   `${data.get('drawdown', 0):,.2f}`\n"
            f"Max loss:   `${data.get('hard_max_loss', 0):,.2f}`\n"
            f"Equity now: `${data.get('current_equity', 0):,.2f}`"
        )
    return f"⚠️ *{event_name.upper()}*\n```{data}```"


def fmt_scanner_row(row: dict) -> str:
    chg  = row.get("postmarket_change") or row.get("premarket_change") or row.get("change_from_open") or 0
    arrow = "▲" if chg >= 0 else "▼"
    return (
        f"`{row.get('name', '?'):<6}` "
        f"`${row.get('close', 0):>7.2f}` "
        f"`{arrow}{abs(chg):.2f}%`"
    )


def fmt_connection_lost(broker_id: str) -> str:
    return f"⚠️ *CONNECTION LOST*\nBroker: `{broker_id}`\n_{dt.datetime.now().strftime('%H:%M:%S')}_"


def fmt_reconnecting(broker_id: str, attempt: int, max_attempts: int) -> str:
    return f"🔄 *Reconnecting…* `{broker_id}` attempt {attempt}/{max_attempts}"


# ── MarkdownV2 formatters (used by CommandHandler / TradingBot) ───────────────
#
# MarkdownV2 requires escaping: _ * [ ] ( ) ~ ` > # + - = | { } . !
# All dynamic values must pass through _esc() before embedding in templates.

import re as _re

def _esc(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return _re.sub(f"([{_re.escape(special)}])", r"\\\1", str(text))


def _pnl_v2(value: float) -> str:
    sign = "🟢 +" if value >= 0 else "🔴 "
    return f"{sign}{_esc(f'{value:,.2f}')}"


def _pct_v2(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return _esc(f"{sign}{value:.2f}%")


def v2_account_status(info: "AccountInfo", broker: str, account: str) -> str:
    equity   = info.current_value    or 0
    cash     = info.cash_in_hand     or 0
    margin   = info.margin_used      or 0
    avail    = info.margin_available or 0
    acct_str = account or getattr(info, "account_id", None) or "default"
    return (
        f"*📊 Account Status*\n"
        f"Broker:   `{_esc(broker)}` / `{_esc(acct_str)}`\n"
        f"Equity:   `{_esc(f'${equity:,.2f}')}`\n"
        f"Cash:     `{_esc(f'${cash:,.2f}')}`\n"
        f"Margin:   `{_esc(f'${margin:,.2f}')}`\n"
        f"Avail:    `{_esc(f'${avail:,.2f}')}`\n"
        f"Leverage: `{_esc(f'{info.leverage:.1f}x')}`"
    )


def v2_positions_list(positions: list, broker: str, account: str) -> str:
    acct_str = account or "default"
    if not positions:
        return (
            f"*📋 Positions*\n"
            f"Broker: `{_esc(broker)}` / `{_esc(acct_str)}`\n"
            f"No open positions"
        )
    lines = [f"*📋 Positions* — `{_esc(broker)}` / `{_esc(acct_str)}`\n"]
    for pos in positions:
        side_emoji = "🟢" if str(pos.side).upper() in ("LONG", "TRADESIDE.LONG") else "🔴"
        upl        = pos.unrealized_pnl or 0.0
        lines.append(
            f"{side_emoji} *{_esc(pos.symbol)}*\n"
            f"  Qty: `{_esc(str(pos.quantity))}`  "
            f"Avg: `{_esc(f'{pos.average_price:,.4f}')}`  "
            f"PnL: {_pnl_v2(upl)}"
        )
    return "\n".join(lines)


def v2_orders_list(orders: list, broker: str) -> str:
    from core.entities.broker_entities import OrderStatus
    pending = [
        o for o in orders
        if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)
    ]
    if not pending:
        return f"*📋 Orders* — `{_esc(broker)}`\nNo pending orders"
    lines = [f"*📋 Orders* — `{_esc(broker)}`\n"]
    for o in pending:
        price = f"{o.price:,.4f}" if getattr(o, "price", None) else "MKT"
        lines.append(
            f"`{_esc(o.symbol)}` {_esc(str(o.side))} "
            f"qty={_esc(str(o.quantity))} "
            f"@ {_esc(price)} "
            f"\\[{_esc(str(o.status))}\\]"
        )
    return "\n".join(lines)


def v2_order_placed(order: "Order", broker: str) -> str:
    return (
        f"*🤖 Order Placed*\n"
        f"Broker: `{_esc(broker)}`\n"
        f"`{_esc(order.symbol)}` {_esc(str(order.side))} "
        f"qty=`{_esc(str(order.quantity))}`\n"
        f"Status: `{_esc(str(order.status))}`\n"
        f"ID: `{_esc(str(order.id))}`"
    )


def v2_risk_status(
    daily_pnl:        float,
    daily_loss_limit: float,
    equity_floor:     float,
    own_equity:       float,
    hard_max_loss:    float,
    starting_equity:  float,
    current_equity:   float,
) -> str:
    drawdown     = starting_equity - current_equity
    floor_ok     = own_equity >= equity_floor
    daily_ok     = daily_pnl > -daily_loss_limit
    drawdown_ok  = drawdown < hard_max_loss

    floor_icon   = "✅" if floor_ok    else "🚨"
    daily_icon   = "✅" if daily_ok   else "⚠️"
    dd_icon      = "✅" if drawdown_ok else "🚨"

    return (
        f"*🛡 Risk Status*\n\n"
        f"{daily_icon} Daily PnL:    {_pnl_v2(daily_pnl)} "
        f"\\(limit: {_esc(f'${daily_loss_limit:,.0f}')}\\)\n"
        f"{floor_icon} Own equity:   `{_esc(f'${own_equity:,.2f}')}` "
        f"\\(floor: {_esc(f'${equity_floor:,.0f}')}\\)\n"
        f"{dd_icon} Drawdown:     `{_esc(f'${drawdown:,.2f}')}` "
        f"\\(max: {_esc(f'${hard_max_loss:,.0f}')}\\)"
    )


def v2_quote(quote: "Quote") -> str:
    spread_pct = float(quote.spread / quote.ask * 100) if quote.ask else 0.0
    ts = quote.timestamp.strftime("%H:%M:%S") if quote.timestamp else "—"
    return (
        f"*📈 Quote — {_esc(quote.symbol)}*\n"
        f"Bid:    `{_esc(f'{float(quote.bid):,.4f}')}` \\({_esc(str(quote.bid_size))}\\)\n"
        f"Ask:    `{_esc(f'{float(quote.ask):,.4f}')}` \\({_esc(str(quote.ask_size))}\\)\n"
        f"Last:   `{_esc(f'{float(quote.last):,.4f}')}`\n"
        f"Mid:    `{_esc(f'{float(quote.mid):,.4f}')}`\n"
        f"Spread: `{_esc(f'{float(quote.spread):,.4f}')}` \\({_esc(f'{spread_pct:.3f}')}%\\)\n"
        f"Time:   `{_esc(ts)}`"
    )


def v2_indicators(symbol: str, tf: str, ts: str, data: dict) -> str:
    atr_val   = data.get("atr")
    atr_pct   = data.get("atr_pct")
    rsi_val   = data.get("rsi")
    st_val    = data.get("st_value")
    st_dir    = data.get("st_dir")
    st_flip   = data.get("st_flipped", False)
    ema8_val  = data.get("ema8")
    ema20_val = data.get("ema20")

    st_emoji    = "🟢" if st_dir == 1 else "🔴"
    st_label    = "Long" if st_dir == 1 else "Short"
    st_flip_tag = " \\[FLIP\\]" if st_flip else ""
    rsi_emoji   = "🔥" if rsi_val and rsi_val >= 70 else ("🧊" if rsi_val and rsi_val <= 30 else "")

    def _n(v, fmt=".2f"): return _esc(f"{v:{fmt}}") if v is not None else "—"

    adx_val = data.get("adx")

    return (
        f"*📊 Indicators — {_esc(symbol)}* `{_esc(tf)}`\n"
        f"`{_esc(ts)}`\n"
        f"{'─' * 30}\n"
        f"ATR        `{_n(atr_val)}` \\({_n(atr_pct, '.2f')}%\\)\n"
        f"RSI        `{_n(rsi_val)}` {rsi_emoji}\n"
        f"ADX 20     `{_n(adx_val, '.1f')}`\n"
        f"EMA 8      `{_n(ema8_val)}`\n"
        f"EMA 20     `{_n(ema20_val)}`\n"
        f"SuperTrend {st_emoji} `{_n(st_val)}` _{_esc(st_label)}_{st_flip_tag}"
    )


def v2_portfolio_indicators(tf: str, results: list, skipped: list) -> str:
    """Compact one-line-per-symbol portfolio indicator table."""
    now    = dt.datetime.utcnow().strftime("%H:%M UTC")
    results = sorted(results, key=lambda r: r[2].get("atr_pct") or 0, reverse=True)
    lines  = [f"*📊 Portfolio — {_esc(tf)} \\+ext* `{_esc(now)}`\n"]

    for ticker, _pos_name, data, _ts in results:
        st_dir   = data.get("st_dir")
        st_emoji = "🟢" if st_dir == 1 else "🔴"
        st_val   = data.get("st_value")
        rsi_val  = data.get("rsi")
        atr_pct  = data.get("atr_pct")
        flip_tag = " \\[F\\]" if data.get("st_flipped") else ""

        def _v(v, fmt=".2f"):
            return _esc(f"{v:{fmt}}") if v is not None else "\\-"

        adx_val   = data.get("adx")
        ema8_val  = data.get("ema8")
        ema20_val = data.get("ema20")
        if ema8_val is not None and ema20_val is not None:
            ema_tag = " `\\(T\\)`" if ema8_val > ema20_val else " `\\(↓\\)`"
        else:
            ema_tag = ""

        lines.append(
            f"*{_esc(ticker)}* {st_emoji} `{_v(st_val)}`{flip_tag}"
            f"  RSI `{_v(rsi_val, '.0f')}`"
            f"  ADX `{_v(adx_val, '.0f')}`"
            f"  ATR `{_v(atr_pct)}`%"
            f"{ema_tag}"
        )

    if skipped:
        names = _esc(", ".join(n for n, _ in skipped[:8]))
        suffix = _esc(f" +{len(skipped) - 8} more") if len(skipped) > 8 else ""
        lines.append(
            f"\n⚠️ _Skipped \\({_esc(str(len(skipped)))}\\): {names}{suffix}_"
        )

    return "\n".join(lines)


def fmt_ml_status(monitors_snapshot: dict, prices: dict) -> str:
    """Format /ml status: current price + distance to each watched level."""
    if not monitors_snapshot:
        return "No active level monitors."
    lines = ["*📍 Level Monitor Status*\n"]
    for sym, entry in monitors_snapshot.items():
        price = prices.get(sym, 0.0)
        price_str = f"{price:,.4f}" if price else "N/A"
        flt_str = ", ".join(f.value for f in entry["filters"]) if entry["filters"] else "all"
        lines.append(f"*{sym}*  `{price_str}`  _({flt_str})_")
        for level in sorted(entry["levels"]):
            if price > 0:
                diff = level - price
                pct  = diff / price * 100
                arrow = "↑" if diff > 0 else "↓"
                lines.append(
                    f"  `{level:.4f}`  {arrow} `{abs(diff):.4f}` ({abs(pct):.2f}%)"
                )
            else:
                lines.append(f"  `{level:.4f}`  _(price unavailable)_")
    return "\n".join(lines)


def v2_error(msg: str) -> str:
    return f"❌ {_esc(msg)}"


def v2_success(msg: str) -> str:
    return f"✅ {_esc(msg)}"


def v2_context_set(broker: str, account: str) -> str:
    acct = f" / `{_esc(account)}`" if account else ""
    return f"✅ Context set: `{_esc(broker)}`{acct}"


def v2_help_text(broker_names: list) -> str:
    brokers_str = _esc(", ".join(broker_names)) if broker_names else _esc("none configured")
    return (
        "*🤖 Trading Bot Commands*\n\n"
        "*Context*\n"
        "`/use [broker] [account]` — set active broker \\+ account\n"
        "`/context` — show current context\n\n"
        "*Read*\n"
        "`/status` — account balance \\& equity\n"
        "`/positions [SYMBOL]` — open positions\n"
        "`/orders` — pending orders\n"
        "`/quote SYMBOL` — bid, ask, last price\n"
        "`/ind SYMBOL [TF]` — ATR, RSI, EMA 8/20, SuperTrend \\(1m 5m 15m 1h 1d\\)\n"
        "`/indp [TF]` — portfolio indicators \\(default 1m \\+ext\\); `ignore`/`unignore`/`list`\n"
        "`/pnl` — today's PnL\n"
        "`/progress` — compound target progress\n"
        "`/risk` — risk limit status\n\n"
        "*Trade*\n"
        "`/buy SYMBOL QTY` — market buy\n"
        "`/sell SYMBOL QTY` — market sell\n"
        "`/close SYMBOL` — close position\n"
        "`/closeall` — close all positions\n"
        "`/stop SYMBOL PRICE` — update stop loss\n\n"
        "*Control*\n"
        "`/halt [SYMBOL]` — halt strategy\n"
        "`/resume [SYMBOL]` — resume strategy\n\n"
        f"*Brokers:* `{brokers_str}`\n"
        "_Add `\\-\\-broker NAME` or `\\-\\-account ID` to any command_"
    )
