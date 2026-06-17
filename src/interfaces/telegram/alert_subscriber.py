"""
interfaces.telegram.alert_subscriber — TelegramAlertSubscriber

Subscribes to:
    ScannerEvent.SYMBOL_DETECTED  → rich scanner-hit alert
    LevelEvent.*                  → price level break / bounce / rejection alert

TradingView URL pattern (uses the user's saved chart layout):
    https://www.tradingview.com/chart/{CHART_ID}/?symbol={EXCHANGE}%3A{SYMBOL}

The chart ID defaults to TV_CHART_ID env var, fallback "3UGuuzJ4".

Exchange map is populated from scanner hits.  Level alerts for newly added
(restored) symbols that haven't been through a scan yet default to "NASDAQ".
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import pytz

from core.adapters.base_subscriber import BaseSubscriber
from core.adapters.event_bus import IEventBus
from core.entities.level_event import LevelEvent, PriceLevelEvent
from core.entities.scanner_event import ScannerEvent, ScannerHit
from core.utils.log_helper import getLogger

if TYPE_CHECKING:
    from infrastructure.cache.redis_cache import RedisCache
    from services.symbol_auto_watcher import SymbolAutoWatcher

_REDIS_SCANNER_SEEN_KEY      = "alert_subscriber"
_REDIS_SCANNER_SEEN_CATEGORY = "telegram_scanner_seen"

logger = getLogger(__name__)

_ET = pytz.timezone("America/New_York")

_SESSION_LABEL = {
    "pre":      "Pre-Market",
    "intraday": "Intraday",
    "post":     "Post-Market",
}

_SCANNER_LABEL = {
    "spikes":      "Spike",
    "pre_market":  "Pre-Market Mover",
    "post_market": "Post-Market Mover",
    "volume":      "High Volume",
}

_LEVEL_EMOJI = {
    LevelEvent.BREAK_ABOVE: "⚡",
    LevelEvent.BREAK_BELOW: "⚡",
    LevelEvent.BOUNCE:      "↩️",
    LevelEvent.REJECTION:   "↩️",
    LevelEvent.FALSE_BREAK: "⚠️",
}

_LEVEL_LABEL = {
    LevelEvent.BREAK_ABOVE: "Break Above",
    LevelEvent.BREAK_BELOW: "Break Below",
    LevelEvent.BOUNCE:      "Bounce",
    LevelEvent.REJECTION:   "Rejection",
    LevelEvent.FALSE_BREAK: "False Break",
}

_DEFAULT_CHART_ID = "3UGuuzJ4"


def _fmt_num(v: float) -> str:
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.1f}M"
    if v >= 1e3:  return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def _fmt_vol(v: float) -> str:
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


def _et_time() -> str:
    return datetime.now(_ET).strftime("%H:%M ET")


def _tv_url(symbol: str, exchange: str, chart_id: str) -> str:
    exch = (exchange or "NASDAQ").upper()
    return f"https://www.tradingview.com/chart/{chart_id}/?symbol={exch}%3A{symbol}"


def _fmt_scanner_hit(hit: ScannerHit, url: str) -> str:
    direction = "▲" if (hit.change_pct or 0) >= 0 else "▼"
    sess_lbl  = _SESSION_LABEL.get(hit.session, hit.session.title())
    scan_lbl  = _SCANNER_LABEL.get(hit.scanner_name, hit.scanner_name.replace("_", " ").title())

    lines = [f"🔥 *{hit.symbol}* — {scan_lbl} ({sess_lbl})"]

    if hit.description:
        lines.append(f"_{hit.description}_")

    lines.append("")
    chg_label = "Pre-Mkt" if hit.session == "pre" else "Day"
    lines.append(f"💰 Price: *${hit.price:.2f}*  |  {chg_label}: `{direction}{abs(hit.change_pct):.2f}%`")

    if hit.session_change_pct is not None:
        sess_dir = "▲" if hit.session_change_pct >= 0 else "▼"
        if hit.session == "pre":
            label = "Pre-Mkt"
        elif hit.session == "post":
            label = "Post-Mkt"
        else:
            label = "From Open"
        lines.append(f"📊 {label}: `{sess_dir}{abs(hit.session_change_pct):.2f}%`")

    vol_parts = []
    if hit.rel_vol is not None:
        vol_parts.append(f"Rel Vol: *{hit.rel_vol:.1f}x*")
    if hit.volume is not None:
        vol_parts.append(f"Vol: {_fmt_vol(hit.volume)}")
    if hit.avg_vol_30d is not None:
        vol_parts.append(f"Avg: {_fmt_vol(hit.avg_vol_30d)}")
    if vol_parts:
        lines.append("📈 " + "  |  ".join(vol_parts))

    meta_parts = []
    if hit.sector:
        meta_parts.append(hit.sector)
    if hit.market_cap:
        meta_parts.append(f"MCap: *{_fmt_num(hit.market_cap)}*")
    if meta_parts:
        lines.append("🏢 " + "  |  ".join(meta_parts))

    lines.append(f"⏰ {_et_time()}")
    lines.append("")
    lines.append(f"[View on TradingView]({url})")

    return "\n".join(lines)


def _fmt_level_event(evt: PriceLevelEvent, url: str) -> str:
    emoji     = _LEVEL_EMOJI.get(evt.event, "📍")
    lbl       = _LEVEL_LABEL.get(evt.event, evt.event.value)
    convincing = "convincing" if evt.convincing else "marginal"

    header_label = f" [{evt.label}]" if evt.label else ""
    lines = [f"{emoji} *{evt.symbol}* — {lbl}{header_label}"]
    lines.append(f"Level: *${evt.level:.2f}*  |  Price: `${evt.price:.2f}`  _{convincing}_")
    lines.append(f"Zone: `[${evt.zone_lo:.2f} – ${evt.zone_hi:.2f}]`  |  ATR: {evt.atr:.3f}")

    extras = []
    if evt.dwell_seconds:
        extras.append(f"Dwell: {evt.dwell_seconds:.0f}s")
    extras.append(f"Source: {evt.tick_source}")
    if extras:
        lines.append("  |  ".join(extras))

    if evt.original_break:
        lines.append(f"Original break: {evt.original_break.value}")

    lines.append(f"⏰ {_et_time()}")
    lines.append("")
    lines.append(f"[View on TradingView]({url})")

    return "\n".join(lines)


class TelegramAlertSubscriber(BaseSubscriber):
    """
    Sends Telegram alerts for scanner hits and price level events.

    exchange_source: optional reference to a SymbolAutoWatcher so the subscriber
    can look up exchange for level events that arrive after restore (before a fresh
    scan hit). If not provided, defaults to "NASDAQ" for all symbols.
    """

    def __init__(
        self,
        bus: IEventBus,
        token: str,
        chat_id: str | int,
        exchange_source: Optional["SymbolAutoWatcher"] = None,
        chart_id: Optional[str] = None,
        cache: Optional["RedisCache"] = None,
    ) -> None:
        super().__init__(bus)
        try:
            from telegram import Bot
            self._bot = Bot(token=token)
        except ImportError:
            raise ImportError("TelegramAlertSubscriber requires 'python-telegram-bot'")

        self._chat_id        = str(chat_id)
        self._exchange_src   = exchange_source
        self._chart_id       = chart_id or os.environ.get("TV_CHART_ID", _DEFAULT_CHART_ID)
        self._cache          = cache
        self._exchange_cache: Dict[str, str] = {}
        self._level_alert_seen: Dict[Tuple[str, str], datetime] = {}  # (symbol, event) → last sent
        self._symbol_last_alert: Dict[str, datetime] = {}             # symbol → last alert of any type
        self._scanner_seen: Dict[str, datetime] = {}                  # symbol → last scanner alert (any scanner)
        self._scanner_seen_loaded = False

        self._subscribe(ScannerEvent.SYMBOL_DETECTED, self._on_scanner_hit)
        for event in LevelEvent:
            self._subscribe(event, self._on_level_event)

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _on_scanner_hit(self, hit: ScannerHit) -> None:
        if not self._scanner_seen_loaded:
            await self._restore_scanner_seen()

        now_et = datetime.now(_ET)
        last = self._scanner_seen.get(hit.symbol)
        if last and (now_et - last).total_seconds() < 86400:
            logger.debug(
                "[TelegramAlertSubscriber] scanner dedup — skipping %s from %s (already alerted today)",
                hit.symbol, hit.scanner_name,
            )
            return
        self._scanner_seen[hit.symbol] = now_et
        await self._persist_scanner_seen(hit.symbol)

        exchange = (hit.exchange or "NASDAQ").upper()
        self._exchange_cache[hit.symbol] = exchange
        url = _tv_url(hit.symbol, exchange, self._chart_id)
        await self._send(_fmt_scanner_hit(hit, url))

    async def _on_level_event(self, evt: PriceLevelEvent) -> None:
        now_et = datetime.now(_ET)
        hour   = now_et.hour + now_et.minute / 60
        in_extended = hour < 9.5 or hour >= 16.0   # pre-market or post-market
        if in_extended and evt.volume < 100_000:
            logger.debug(
                "[TelegramAlertSubscriber] skipping %s %s — volume %.0f < 100K min (extended session)",
                evt.symbol, evt.event.value, evt.volume,
            )
            return

        # 1H S/R alerts are too noisy for now — logged by KeyLevelMonitorService
        if evt.label in ("1H Support", "1H Resistance"):
            return

        # Drop non-convincing signals (marginal breaks/bounces add noise)
        if not evt.convincing:
            logger.debug(
                "[TelegramAlertSubscriber] skipping %s %s — not convincing",
                evt.symbol, evt.event.value,
            )
            return

        # Drop tiny-ATR stocks — zone is near-zero, any tick fires an event
        if evt.atr < 0.05:
            logger.debug(
                "[TelegramAlertSubscriber] skipping %s %s — ATR %.4f below min",
                evt.symbol, evt.event.value, evt.atr,
            )
            return

        # Per-symbol cooldown: max 1 alert per symbol per 5 minutes across all event types
        sym_last = self._symbol_last_alert.get(evt.symbol)
        if sym_last and (now_et - sym_last) < timedelta(seconds=300):
            logger.debug(
                "[TelegramAlertSubscriber] symbol cooldown — skipping %s %s (last alert %.0fs ago)",
                evt.symbol, evt.event.value, (now_et - sym_last).total_seconds(),
            )
            return

        # Per-(symbol, event) cooldown: same event type suppressed for 10 minutes
        dedup_key = (evt.symbol, evt.event.value)
        last_sent = self._level_alert_seen.get(dedup_key)
        if last_sent and (now_et - last_sent) < timedelta(seconds=600):
            logger.debug(
                "[TelegramAlertSubscriber] dedup — skipping %s %s (sent %.0fs ago)",
                evt.symbol, evt.event.value, (now_et - last_sent).total_seconds(),
            )
            return
        self._level_alert_seen[dedup_key] = now_et
        self._symbol_last_alert[evt.symbol] = now_et

        exchange = self._resolve_exchange(evt.symbol)
        url = _tv_url(evt.symbol, exchange, self._chart_id)
        await self._send(_fmt_level_event(evt, url))

    # ── Redis persistence for scanner-seen dedup ──────────────────────────────

    async def _restore_scanner_seen(self) -> None:
        self._scanner_seen_loaded = True
        if self._cache is None:
            return
        try:
            data = await self._cache.load(_REDIS_SCANNER_SEEN_KEY, category=_REDIS_SCANNER_SEEN_CATEGORY)
            if isinstance(data, dict):
                for symbol, ts_str in data.items():
                    try:
                        self._scanner_seen[symbol] = datetime.fromisoformat(ts_str)
                    except ValueError:
                        pass
                logger.info(
                    "[TelegramAlertSubscriber] restored %d scanner-seen symbols from Redis",
                    len(self._scanner_seen),
                )
        except Exception as e:
            logger.warning("[TelegramAlertSubscriber] could not restore scanner_seen: %s", e)

    async def _persist_scanner_seen(self, symbol: str) -> None:
        if self._cache is None:
            return
        try:
            data = {s: t.isoformat() for s, t in self._scanner_seen.items()}
            await self._cache.save(
                _REDIS_SCANNER_SEEN_KEY, data,
                category=_REDIS_SCANNER_SEEN_CATEGORY,
                ttl=86400,
            )
        except Exception as e:
            logger.warning("[TelegramAlertSubscriber] could not persist scanner_seen: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_exchange(self, symbol: str) -> str:
        if symbol in self._exchange_cache:
            return self._exchange_cache[symbol]
        if self._exchange_src is not None:
            exch = self._exchange_src.exchange_map.get(symbol)
            if exch:
                self._exchange_cache[symbol] = exch
                return exch
        return "NASDAQ"

    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("[TelegramAlertSubscriber] send failed")
