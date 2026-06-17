"""
services.symbol_auto_watcher — SymbolAutoWatcher

Subscribes to ScannerEvent.SYMBOL_DETECTED and wires each new symbol into the
live price and level-monitoring pipelines.

File format (data/watched_symbols.json):
    {
      "portfolio": {
        "scalable": ["APLD", "PLTR"],
        "ibkr":     [],
        "etoro":    ["NVDA"],
        "capital":  []
      },
      "scanners": {
        "CLPT": {"date": "2026-06-17", "time": "07:25", "exchange": "NASDAQ",
                 "scanners": ["pre_market"], "rel_vol": 5.2, "change_pct": 12.0}
      }
    }

Portfolio section is synced from live brokers at startup; falls back to the
last persisted state when a broker is unavailable. Portfolio symbols are always
monitored — they bypass the rel_vol/change quality filter and the max_monitored cap.

Scanner symbols (today only) are restored with quality filtering:
    rel_vol >= min_rel_vol  OR  |change_pct| >= min_change_pct
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

import pytz

from core.adapters.base_subscriber import BaseSubscriber
from core.adapters.event_bus import IEventBus
from core.entities.scanner_event import ScannerEvent, ScannerHit
from core.utils.log_helper import getLogger

if TYPE_CHECKING:
    from services.key_level_monitor_service import KeyLevelMonitorService
    from services.price_monitor import PriceMonitor

logger = getLogger(__name__)

_ET           = pytz.timezone("America/New_York")
_PERSIST_FILE = Path(__file__).parents[2] / "data" / "watched_symbols.json"


class SymbolAutoWatcher(BaseSubscriber):

    def __init__(
        self,
        bus: IEventBus,
        price_monitor: "PriceMonitor",
        key_level_monitor: "KeyLevelMonitorService",
        max_monitored: int = 30,
        min_rel_vol: float = 4.0,
        min_change_pct: float = 5.0,
    ) -> None:
        super().__init__(bus)
        self._pm   = price_monitor
        self._klm  = key_level_monitor
        self._max_monitored  = max_monitored
        self._min_rel_vol    = min_rel_vol
        self._min_change_pct = min_change_pct

        # symbols currently subscribed to price+level pipeline
        self._watched: set[str] = set()

        # portfolio: broker_id → list of symbols from live positions
        self._portfolio: Dict[str, List[str]] = {}
        self._portfolio_symbols: set[str] = set()

        # scanner metadata
        self._exchange: Dict[str, str] = {}
        self._scanners: Dict[str, List[str]] = {}
        self._times:    Dict[str, str]  = {}
        self._rel_vols: Dict[str, float] = {}
        self._changes:  Dict[str, float] = {}

        self._subscribe(ScannerEvent.SYMBOL_DETECTED, self._on_hit)

    # ── Public ────────────────────────────────────────────────────────────────

    @property
    def exchange_map(self) -> Dict[str, str]:
        return self._exchange

    async def sync_portfolio(self, broker_id: str, broker) -> None:
        """
        Sync portfolio symbols from a live broker connection.
        Adds newly held symbols to monitoring; updates the portfolio section.
        If broker.get_positions() fails, the last persisted state is kept.
        """
        try:
            positions = await broker.get_positions()
            symbols   = [p.symbol.upper() for p in positions if p.symbol]
            self._portfolio[broker_id] = symbols
            logger.info(
                "[SymbolAutoWatcher] portfolio sync [%s]: %d positions — %s",
                broker_id, len(symbols), symbols,
            )
        except Exception as e:
            logger.warning(
                "[SymbolAutoWatcher] portfolio sync [%s] failed (%s) — keeping last state",
                broker_id, e,
            )
            # Preserve whatever was loaded from file
            return

        self._portfolio_symbols = {
            s for syms in self._portfolio.values() for s in syms
        }

        for symbol in symbols:
            if symbol not in self._watched:
                await self._subscribe_symbol(symbol, source=f"portfolio:{broker_id}")

        self._persist()

    async def restore_today(self) -> None:
        """
        Load portfolio (all brokers, any date — last known state) and today's
        scanner symbols (quality-filtered) into the monitoring pipeline.
        """
        data     = self._load_persist()
        today_et = date.today().isoformat()

        # ── Portfolio: always restore, no date filter, no quality gate ────────
        portfolio_data = data.get("portfolio", {})
        self._portfolio = {k: list(v) for k, v in portfolio_data.items()}
        self._portfolio_symbols = {
            s for syms in self._portfolio.values() for s in syms
        }
        p_restored = 0
        for symbol in self._portfolio_symbols:
            if symbol not in self._watched:
                await self._subscribe_symbol(symbol, source="portfolio")
                p_restored += 1
        if p_restored:
            logger.info("[SymbolAutoWatcher] restored %d portfolio symbol(s)", p_restored)

        # ── Scanners: today only, quality-filtered ────────────────────────────
        scanner_data = data.get("scanners", {})
        s_restored = 0
        for symbol, meta in scanner_data.items():
            if meta.get("date") != today_et:
                continue
            if symbol in self._watched:
                continue
            if len(self._watched) - len(self._portfolio_symbols) >= self._max_monitored:
                logger.info(
                    "[SymbolAutoWatcher] scanner restore cap reached (%d)", self._max_monitored
                )
                break
            rel_vol    = meta.get("rel_vol")
            change_pct = meta.get("change_pct")
            if not self._passes_quality(rel_vol, change_pct):
                logger.debug(
                    "[SymbolAutoWatcher] restore skip %s — low activity "
                    "(rel_vol=%s, chg=%s%%)", symbol, rel_vol, change_pct,
                )
                continue
            self._exchange[symbol] = meta.get("exchange", "NASDAQ")
            self._scanners[symbol] = meta.get("scanners", [])
            self._times[symbol]    = meta.get("time", "")
            self._rel_vols[symbol] = float(rel_vol) if rel_vol is not None else 0.0
            self._changes[symbol]  = float(change_pct) if change_pct is not None else 0.0
            await self._subscribe_symbol(symbol, source="scanner-restore")
            s_restored += 1

        if s_restored:
            logger.info("[SymbolAutoWatcher] restored %d scanner symbol(s) from today", s_restored)

    # ── Handler ───────────────────────────────────────────────────────────────

    async def _on_hit(self, hit: ScannerHit) -> None:
        scanner  = hit.scanner_name or "unknown"
        exchange = (hit.exchange or "NASDAQ").upper()
        now_et   = datetime.now(_ET).strftime("%H:%M")

        # Portfolio symbols bypass all filters — already subscribed from restore/sync
        if hit.symbol in self._portfolio_symbols:
            self._exchange.setdefault(hit.symbol, exchange)
            return

        if hit.symbol not in self._watched:
            scanner_count = len(self._watched) - len(self._portfolio_symbols & self._watched)
            at_cap      = scanner_count >= self._max_monitored
            passes_qual = self._passes_quality(hit.rel_vol, hit.change_pct)

            if at_cap or not passes_qual:
                reason = f"cap={self._max_monitored}" if at_cap else \
                         f"low activity (rel_vol={hit.rel_vol}, chg={hit.change_pct:.1f}%)"
                logger.debug(
                    "[SymbolAutoWatcher] %s — Telegram alert only, skipping monitoring (%s)",
                    hit.symbol, reason,
                )
                self._exchange.setdefault(hit.symbol, exchange)
            else:
                self._exchange[hit.symbol] = exchange
                self._scanners[hit.symbol] = [scanner]
                self._times[hit.symbol]    = now_et
                self._rel_vols[hit.symbol] = hit.rel_vol or 0.0
                self._changes[hit.symbol]  = abs(hit.change_pct or 0.0)
                await self._subscribe_symbol(hit.symbol, source=scanner)
        else:
            scanners = self._scanners.setdefault(hit.symbol, [])
            if scanner not in scanners:
                scanners.append(scanner)

        self._persist()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _subscribe_symbol(self, symbol: str, source: str) -> None:
        self._watched.add(symbol)
        try:
            await self._pm.subscribe(symbol)
        except Exception:
            logger.exception("[SymbolAutoWatcher] PriceMonitor.subscribe failed for %s", symbol)
        try:
            await self._klm.add_symbol(symbol)
        except Exception:
            logger.exception("[SymbolAutoWatcher] KeyLevelMonitor.add_symbol failed for %s", symbol)
        logger.info("[SymbolAutoWatcher] monitoring %s  source=%s", symbol, source)

    def _passes_quality(self, rel_vol: Optional[float], change_pct: Optional[float]) -> bool:
        if rel_vol is not None and rel_vol >= self._min_rel_vol:
            return True
        if change_pct is not None and abs(change_pct) >= self._min_change_pct:
            return True
        return rel_vol is None and change_pct is None

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        today_et = date.today().isoformat()
        existing = self._load_persist()

        # Merge portfolio
        existing_portfolio = existing.get("portfolio", {})
        for broker_id, symbols in self._portfolio.items():
            existing_portfolio[broker_id] = symbols
        existing["portfolio"] = existing_portfolio

        # Merge scanners (today only)
        existing_scanners = {
            sym: meta
            for sym, meta in existing.get("scanners", {}).items()
            if meta.get("date") == today_et
        }
        for symbol in self._watched:
            if symbol in self._portfolio_symbols:
                continue  # portfolio symbols don't go in scanners section
            existing_scanners[symbol] = {
                "date":       today_et,
                "time":       self._times.get(symbol, ""),
                "exchange":   self._exchange.get(symbol, "NASDAQ"),
                "scanners":   self._scanners.get(symbol, []),
                "rel_vol":    self._rel_vols.get(symbol),
                "change_pct": self._changes.get(symbol),
            }
        existing["scanners"] = existing_scanners

        try:
            _PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PERSIST_FILE.write_text(json.dumps(existing, indent=2))
        except Exception:
            logger.warning("[SymbolAutoWatcher] could not write %s", _PERSIST_FILE)

    def _load_persist(self) -> dict:
        try:
            if _PERSIST_FILE.exists():
                raw = json.loads(_PERSIST_FILE.read_text())
                # Migrate old flat format → new format
                if raw and "portfolio" not in raw and "scanners" not in raw:
                    return {"portfolio": {}, "scanners": raw}
                return raw
        except Exception:
            logger.warning("[SymbolAutoWatcher] could not read %s", _PERSIST_FILE)
        return {"portfolio": {}, "scanners": {}}
