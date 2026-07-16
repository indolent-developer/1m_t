"""
services.news_reaction_analyzer — NewsReactionAnalyzer

Watches price action after each new news item and fires a Telegram alert
the moment the stock moves ≥ 2 % (either direction) from the price at
the time the news was published.

Flow
----
  1. On NewsEvent.NEWS_PUBLISHED:
       • Record price_at_news from the FMP 1-minute historical bar covering
         the article's published_date — NOT the current streaming tick,
         which by the time this handler runs may be minutes stale relative
         to `published_date` (see StockNews.latency_seconds). Falls back to
         the last streaming tick, then a live FMP snapshot, if the
         historical lookup comes back empty.
       • Push a PendingWatch onto the 30-item FIFO deque.

  2. On BrokerEvent.QUOTE_UPDATE (streaming tick for watchlist symbols):
       • Update _last_prices.
       • Check every active PendingWatch for that symbol.

  3. Background poll loop (every 30 s):
       • For symbols NOT on the watchlist (discovered via FMP global news feed),
         fetch price from FMP and apply the same check.

  4. When |move| ≥ 2 %:
       • Send Telegram alert.
       • Emit ScannerEvent.SYMBOL_DETECTED so SymbolAutoWatcher adds the
         symbol to the key-level monitor.
       • Mark watch as alerted (won't fire again for the same news item).
       • Remove the symbol from the FMP poll queue (if it was in it).

FIFO cap
--------
  At most 30 PendingWatch objects are active at any time.  When a 31st
  arrives the oldest is silently dropped (collections.deque maxlen=30).

Window
------
  Each PendingWatch expires 60 minutes after the article's published_at.
  Moves detected after expiry are ignored.  A cleanup task purges stale
  watches every 5 minutes.
"""
from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Deque, Dict, Optional, Set

import pytz

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.adapters.base_subscriber import BaseSubscriber
from core.adapters.event_bus import IEventBus
from core.entities.market_data import PriceTick, StockNews
from core.entities.news_event import NewsEvent
from core.entities.scanner_event import ScannerEvent, ScannerHit
from core.entities.time_frame import TimeFrame
from core.utils.log_helper import getLogger

if TYPE_CHECKING:
    from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher

logger = getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_MOVE_THRESHOLD   = 0.02      # 2 %
_WATCH_WINDOW_MIN = 60        # expire after 60 minutes
_MAX_WATCHES      = 30        # FIFO cap
_POLL_INTERVAL_S  = 30        # FMP price poll cadence for untracked symbols

# Same TradingView chart link every other alert card ends with
# (interfaces.telegram.alert_subscriber._tv_url) — duplicated here rather than
# imported to keep services/ from depending on interfaces/telegram/.
_DEFAULT_CHART_ID = "3UGuuzJ4"


def _tv_url(symbol: str, chart_id: str) -> str:
    return f"https://www.tradingview.com/chart/{chart_id}/?symbol={symbol}"
_CLEANUP_INTERVAL_S = 300     # clean up expired watches every 5 min


@dataclass
class PendingWatch:
    symbol:        str
    news:          StockNews
    price_at_news: float
    expires_at:    datetime
    alerted:       bool = False
    # Claimed synchronously (no `await` in between) by the first tick that
    # crosses the threshold, before bar confirmation runs. Without this,
    # concurrent ticks from IBKR/Finnhub/FMP (all live in `source: auto`)
    # can each see `alerted == False` during the confirmation `await` window
    # and each independently fire — see docs/NewsCatalystAlert.md §6a.
    checking:      bool = False


class NewsReactionAnalyzer(BaseSubscriber):
    """
    Subscribe to live news + price ticks; alert when a news catalyst moves
    a stock ≥ 2 % within 60 minutes of publication.

    Parameters
    ----------
    bus           : event bus (subscribed to NewsEvent.NEWS_PUBLISHED + QUOTE_UPDATE)
    fmp           : FmpDataFetcher — used for price snapshots on untracked symbols
    telegram_token: Telegram bot token (None = no Telegram alerts)
    chat_id       : Telegram chat / channel ID
    watchlist     : set of symbols currently on the live price stream;
                    symbols NOT in this set go to the FMP poll queue.
                    Pass the SymbolAutoWatcher._watched set or a copy.
    """

    def __init__(
        self,
        bus: IEventBus,
        fmp: "FmpDataFetcher",
        telegram_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        watchlist: Optional[Set[str]] = None,
        chart_id: Optional[str] = None,
    ) -> None:
        super().__init__(bus)

        self._fmp         = fmp
        self._chat_id     = str(chat_id) if chat_id else None
        self._watchlist   = watchlist if watchlist is not None else set()
        self._chart_id    = chart_id or os.environ.get("TV_CHART_ID", _DEFAULT_CHART_ID)

        # FIFO watch queue — maxlen enforces the 30-item cap
        self._watches: Deque[PendingWatch] = deque(maxlen=_MAX_WATCHES)

        # Last known price per symbol (updated from QUOTE_UPDATE ticks)
        self._last_prices: Dict[str, float] = {}

        # Symbols not on the streaming watchlist that need FMP polling
        self._poll_symbols: Set[str] = set()

        self._bot = None
        if telegram_token:
            try:
                from telegram import Bot
                self._bot = Bot(token=telegram_token)
            except ImportError:
                logger.warning("[NewsReactionAnalyzer] python-telegram-bot not installed — no Telegram alerts")

        self._subscribe(NewsEvent.NEWS_PUBLISHED, self._on_news)
        self._subscribe(BrokerEvent.QUOTE_UPDATE, self._on_tick)

        self._poll_task:    Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._poll_task    = asyncio.create_task(self._fmp_poll_loop(),  name="news_rx_poll")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(),   name="news_rx_cleanup")

    def stop(self) -> None:
        for t in (self._poll_task, self._cleanup_task):
            if t:
                t.cancel()

    # ── Event handlers ────────────────────────────────────────────────────────

    async def _on_news(self, item: StockNews) -> None:
        symbol = (item.symbol or "").upper().strip()
        if not symbol:
            return

        pub = item.published_date
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)

        # Prefer the price actually printed at publish time (1-min historical
        # bar) over whatever streaming tick happens to be on hand right now —
        # NEWS_PUBLISHED can arrive minutes after `pub` (see latency_seconds),
        # by which point "current" price no longer reflects "price at news".
        price = await self._price_at_publish(symbol, pub)
        if price is None:
            price = self._last_prices.get(symbol)
        if price is None:
            try:
                loop = asyncio.get_event_loop()
                price = await loop.run_in_executor(
                    None, lambda: self._fmp.get_last_price(symbol)
                )
            except Exception as exc:
                logger.warning("[NewsReactionAnalyzer] price snapshot failed for %s: %s", symbol, exc)

        if not price:
            logger.debug("[NewsReactionAnalyzer] %s — no price available, skipping watch", symbol)
            return

        expires = pub + timedelta(minutes=_WATCH_WINDOW_MIN)

        watch = PendingWatch(
            symbol=symbol,
            news=item,
            price_at_news=price,
            expires_at=expires,
        )
        self._watches.append(watch)   # oldest auto-dropped if at maxlen

        if symbol not in self._watchlist:
            self._poll_symbols.add(symbol)

        logger.info(
            "[NewsReactionAnalyzer] watching %s — price=%.2f  source=%s  expires=%s  '%s'",
            symbol, price, item.news_source,
            expires.astimezone(_ET).strftime("%H:%M ET"),
            item.title[:55],
        )

    async def _price_at_publish(self, symbol: str, pub: datetime) -> Optional[float]:
        """
        Look up the close of the 1-minute bar covering `pub` from FMP
        historical-chart data. IBKR is push-only in this codebase (no
        historical-bar endpoint), so FMP is the only source for a real
        as-of-time lookup.
        """
        try:
            loop = asyncio.get_event_loop()
            bars = await loop.run_in_executor(
                None,
                lambda: self._fmp.get_market_data(
                    symbol,
                    pub - timedelta(minutes=5),
                    pub + timedelta(minutes=1),
                    TimeFrame.MINUTE_1,
                    use_cache=False,
                ),
            )
        except Exception as exc:
            logger.debug("[NewsReactionAnalyzer] historical price lookup failed for %s: %s", symbol, exc)
            return None
        if not bars:
            return None

        pub_et = pub.astimezone(_ET).replace(tzinfo=None)
        prior = [b for b in bars if b.time and b.time <= pub_et]
        bar = prior[-1] if prior else bars[0]
        return bar.close

    async def _on_tick(self, payload: BrokerEventPayload) -> None:
        if not isinstance(payload.data, PriceTick):
            return
        tick = payload.data
        self._last_prices[tick.symbol] = tick.price
        await self._check_symbol(tick.symbol, tick.price)

    # ── Price check ───────────────────────────────────────────────────────────

    async def _check_symbol(self, symbol: str, price: float) -> None:
        now = datetime.now(timezone.utc)
        for watch in self._watches:
            if watch.symbol != symbol or watch.alerted or watch.checking:
                continue
            if now > watch.expires_at:
                continue
            move = (price - watch.price_at_news) / watch.price_at_news
            if abs(move) < _MOVE_THRESHOLD:
                continue

            # Claim the watch *synchronously* (no await yet) so a concurrent
            # tick from another source (IBKR/Finnhub/FMP all run in parallel
            # under `source: auto`) can't also see alerted=False and race us
            # into a duplicate confirmation + alert while we're awaiting below.
            watch.checking = True
            try:
                # A single streaming tick isn't enough to alert on — FMP's feed
                # occasionally emits a stale/erroneous print during thin premarket
                # liquidity (see docs/FmpAlertLatency.md), which can look like a
                # huge move for one tick and revert seconds later. Confirm the
                # move against the latest FMP 1-minute bar close before firing.
                confirmed_price = await self._confirm_via_bar(symbol)
                if confirmed_price is None:
                    continue
                confirmed_move = (confirmed_price - watch.price_at_news) / watch.price_at_news
                if abs(confirmed_move) >= _MOVE_THRESHOLD:
                    await self._on_reaction(watch, confirmed_price, confirmed_move, now)
            finally:
                watch.checking = False

    async def _confirm_via_bar(self, symbol: str) -> Optional[float]:
        """Latest FMP 1-minute bar close — used to confirm a tick-detected move."""
        now = datetime.now(timezone.utc)
        try:
            loop = asyncio.get_event_loop()
            bars = await loop.run_in_executor(
                None,
                lambda: self._fmp.get_market_data(
                    symbol,
                    now - timedelta(minutes=5),
                    now,
                    TimeFrame.MINUTE_1,
                    use_cache=False,
                ),
            )
        except Exception as exc:
            logger.debug("[NewsReactionAnalyzer] bar confirmation failed for %s: %s", symbol, exc)
            return None
        if not bars:
            return None
        return bars[-1].close

    async def _on_reaction(
        self,
        watch: PendingWatch,
        price: float,
        move: float,
        now: datetime,
    ) -> None:
        watch.alerted = True
        self._poll_symbols.discard(watch.symbol)

        elapsed_min = int((now - watch.expires_at + timedelta(minutes=_WATCH_WINDOW_MIN)).total_seconds() / 60)
        direction   = "+" if move > 0 else ""
        pub_et      = watch.news.published_date
        if pub_et.tzinfo is None:
            pub_et = pub_et.replace(tzinfo=timezone.utc)
        pub_str     = pub_et.astimezone(_ET).strftime("%H:%M ET")

        lat = watch.news.latency_seconds
        lat_str = f", latency {lat / 60:.1f}m" if lat is not None else ""
        source_str = f"[{watch.news.news_source}{lat_str}]" if watch.news.news_source else ""

        url = _tv_url(watch.symbol, self._chart_id)
        msg = (
            f"📰 *News Catalyst: {watch.symbol}*\n"
            f"Move: `{direction}{move*100:.1f}%` in {elapsed_min}m after news\n"
            f"News: \"{watch.news.title[:55]}...\" {source_str}\n"
            f"Published: {pub_str}\n"
            f"Price at news: `${watch.price_at_news:.2f}` → Now: `${price:.2f}`\n"
            f"⏰ {now.astimezone(_ET).strftime('%H:%M ET')}\n"
            f"\n"
            f"{url}"
        )

        logger.info(
            "[NewsReactionAnalyzer] REACTION %s  %s%.1f%%  elapsed=%dm  news='%s'",
            watch.symbol, direction, abs(move * 100), elapsed_min, watch.news.title[:40],
        )

        await self._send(msg)
        await self._add_to_watchlist(watch, price, move)

    async def _add_to_watchlist(self, watch: PendingWatch, price: float, move: float) -> None:
        hit = ScannerHit(
            event=ScannerEvent.SYMBOL_DETECTED,
            symbol=watch.symbol,
            scanner_name="news_catalyst",
            session="intraday",
            price=price,
            change_pct=round(move * 100, 2),
            description=watch.news.title[:80],
        )
        try:
            await self._bus.emit(hit)
        except Exception as exc:
            logger.warning("[NewsReactionAnalyzer] could not emit ScannerHit for %s: %s", watch.symbol, exc)

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _fmp_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            for symbol in list(self._poll_symbols):
                try:
                    loop = asyncio.get_event_loop()
                    price = await loop.run_in_executor(
                        None, lambda s=symbol: self._fmp.get_last_price(s)
                    )
                    if price:
                        self._last_prices[symbol] = price
                        await self._check_symbol(symbol, price)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.debug("[NewsReactionAnalyzer] FMP poll error for %s: %s", symbol, exc)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL_S)
            now = datetime.now(timezone.utc)
            stale = [w for w in self._watches if w.alerted or now > w.expires_at]
            for w in stale:
                try:
                    self._watches.remove(w)
                except ValueError:
                    pass
                if not any(x.symbol == w.symbol for x in self._watches):
                    self._poll_symbols.discard(w.symbol)
            if stale:
                logger.debug("[NewsReactionAnalyzer] cleaned up %d stale watches", len(stale))

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        if self._bot is None or self._chat_id is None:
            return
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("[NewsReactionAnalyzer] Telegram send failed")
