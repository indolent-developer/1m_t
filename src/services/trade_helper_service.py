"""
services.trade_helper_service

Deep pre-trade analysis for a symbol.  Layers collected in parallel:
  1. Company profile  (FundamentalsService / FMP)
  2. Daily OHLCV + technical indicators  (ATR, RSI, ADX, EMA, SuperTrend)
  3. Market context  (SPY, QQQ, VIX via Yahoo Finance)
  4. News + hard-event detection  (NewsService)
  5. Next earnings date  (Finnhub)
  6. LLM synthesis  (GrokLLM — grok-3 by default)

Returns a TradeHelperReport dataclass with a ENTER / AVOID / WATCH verdict.
If XAI_API_KEY is absent, verdict is rule-based (free).

Cache TTLs (in-process MemoryCache):
  full report    — 5 min   (avoids double LLM calls)
  market context — 5 min   (SPY/QQQ/VIX tick continuously)
  OHLCV bars     — 15 min  (daily candle incomplete until close)
  news/events    — 10 min  (breaking news matters, but not sub-minute)
  earnings date  — 60 min  (quarterly data, very stable)

Usage:
    svc    = TradeHelperService()
    report = await svc.analyse("AAPL")
    print(report.verdict, report.side_bias, report.confidence)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from core.utils.log_helper import getLogger
import os
import re
from dataclasses import dataclass, field

import pandas as pd

from infrastructure.cache.memory_cache import MemoryCache
from services.fundamentals_service import FundamentalsService
from services.news_service import NewsService
import services.indicators_service as ind

# ── Cache TTLs (seconds) ──────────────────────────────────────────────────────
_TTL_REPORT  =  5 * 60   # full TradeHelperReport
_TTL_MKT_CTX =  5 * 60   # SPY / QQQ / VIX snapshot
_TTL_OHLCV   = 15 * 60   # daily OHLCV bars
_TTL_NEWS    = 10 * 60   # news + hard events
_TTL_EARNINGS = 60 * 60       # next earnings date
_TTL_ECON_CAL =  4 * 60 * 60  # macro economic calendar (same for all symbols)
_TTL_CORR     = 24 * 60 * 60  # 60d return correlations — only changes at EOD

# Intraday TTL = one candle duration (data can't be fresher than the candle)
_TTL_INTRADAY: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900}
_INTRADAY_TFS = ("1m", "5m", "15m")
# How many bars to request from FMP for each intraday ST timeframe (incl. extended)
_FMP_ST_LIMIT: dict[str, int] = {"1m": 300, "5m": 200, "15m": 150}

# Bar CSV fetch (5m + 1h for LLM context) — FMP limits and TTLs
_FMP_BARS_LIMIT: dict[str, int] = {"5m": 100, "1h": 40}
_TTL_BARS:       dict[str, int] = {"5m": 300, "1h": 3_600}

logger = getLogger(__name__)

_SPY = "SPY"
_QQQ = "QQQ"
_VIX = "^VIX"

_VIX_REGIMES = [
    (12,   "very_low"),
    (18,   "low"),
    (25,   "elevated"),
    (35,   "high"),
    (9999, "extreme"),
]

_HARD_EVENT_PATTERNS: dict[str, tuple[str, float]] = {
    r"equity offering|share offering|dilut|at-the-market|ATM offering|secondary offering": ("offering",      -0.9),
    r"indict|fraud charge|SEC charges|DOJ|criminal":                                        ("legal",         -0.9),
    r"delist|going concern|auditor resign":                                                 ("accounting",    -1.0),
    r"chapter 11|bankrupt":                                                                 ("bankruptcy",    -1.0),
    r"guidance cut|cuts guidance|lowers (outlook|forecast)|misses estimates|revenue miss":  ("guidance_down", -0.7),
    r"raises guidance|boosts (outlook|forecast)|beats estimates":                           ("guidance_up",    0.7),
    r"acquisition of|to acquire|merger agreement|buyout|takeover bid":                      ("ma",             0.6),
    r"buyback|share repurchase":                                                             ("buyback",        0.5),
    r"upgrade[sd]? to (buy|overweight|outperform)":                                         ("upgrade",        0.4),
    r"downgrade[sd]? to (sell|underweight|underperform)":                                   ("downgrade",     -0.4),
}


def _vix_regime(v: float) -> str:
    for threshold, label in _VIX_REGIMES:
        if v <= threshold:
            return label
    return "extreme"


def _format_st_compact(intraday: dict[str, dict], daily_tech: dict) -> str:
    """Compact one-liner for LLM prompt: '1m:bull 5m:bull 15m:bear 1d:bull'."""
    parts = []
    for tf in _INTRADAY_TFS:
        s = intraday.get(tf, {})
        if s:
            lbl  = "bull" if s["direction"] == 1 else "bear"
            flip = "!" if s.get("flipped") else ""
            parts.append(f"{tf}:{lbl}{flip}")
    st_dir = daily_tech.get("st_dir")
    if st_dir is not None:
        lbl  = "bull" if st_dir == 1 else "bear"
        flip = "!" if daily_tech.get("st_flipped") else ""
        parts.append(f"1d:{lbl}{flip}")
    return "  ".join(parts)


def _ohlcv_to_csv(df: pd.DataFrame, n: int = 20) -> str:
    """Last n bars as compact CSV rows (no header): 2dp prices, int vol."""
    rows  = df.tail(n)
    lines = []
    for _, row in rows.iterrows():
        t  = row["t"]
        ts = t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[:16]
        lines.append(
            f"{ts},{row['o']:.2f},{row['h']:.2f},{row['l']:.2f},{row['c']:.2f},{int(row['v'])}"
        )
    return "\n".join(lines)


def _session_vol_ratio(df_5m: pd.DataFrame, adv20: float) -> str:
    """Today's cumulative 5m volume vs expected pace (bare number, no 'x')."""
    if adv20 <= 0 or df_5m.empty:
        return "—"
    try:
        t_col = df_5m["t"]
        last  = t_col.iloc[-1]
        today = last.date() if hasattr(last, "date") else pd.Timestamp(last).date()
        mask  = t_col.apply(lambda x: (x.date() if hasattr(x, "date") else pd.Timestamp(x).date()) == today)
        today_bars = df_5m[mask]
        n_bars     = len(today_bars)
        if n_bars == 0:
            return "—"
        today_vol = float(today_bars["v"].sum())
        expected  = adv20 * n_bars / 78.0   # 78 × 5m bars ≈ full 6.5h session
        return f"{today_vol / expected:.1f}" if expected > 0 else "—"
    except Exception:
        return "—"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class MarketContext:
    spy_price:      float
    spy_ret_1d:     float
    spy_ret_5d:     float
    spy_ret_20d:    float
    spy_above_200d: bool
    vix:            float
    vix_regime:     str
    qqq_ret_1d:     float
    qqq_ret_5d:     float


@dataclass
class TradeHelperReport:
    symbol:       str
    timestamp:    str
    verdict:      str          # ENTER / AVOID / WATCH
    side_bias:    str          # long / short / neutral
    confidence:   str          # high / medium / low

    company_name: str
    sector:       str
    industry:     str
    market_cap:   float | None
    beta:         float | None
    description:  str

    price:        float
    atr:          float
    atr_pct:      float

    technicals:      dict
    intraday_st:     dict[str, dict]   # tf → {value, direction, flipped, dist_pct}
    market_context:  MarketContext
    news_events:     list[dict]
    news_items:      list[dict]        # {title, ts, source} — for display with timestamps
    earnings_days:   int | None

    bull_case:    list[str]
    bear_case:    list[str]
    key_risks:    list[str]
    llm_synthesis: str

    entry_ref:    float | None
    stop:         float | None
    stop_basis:   str | None
    target:       float | None
    targets_list: list[dict]           # [{level, basis}, …] from LLM
    rr:           float | None
    sizing:       dict | None          # shares, notional, risk_usd, binding, sector gate

    # New LLM schema fields
    timeframe:    str | None           # scalp | intraday | swing
    sentiment:    dict | None          # {score: float, driver: str}
    entry_zone:   dict | None          # {zone_low, zone_high, trigger}
    watch_for:    str | None           # WATCH condition that upgrades to ENTER
    hold_plan:    dict | None          # {horizon, carry_condition}
    level_read:   str | None           # 1-sentence key structure from bars
    portfolio_fit: dict | None         # {effect, note} from LLM
    validation_issues: list[str]       # from validate_card — empty = levels are clean

    # Debug / data-quality fields
    last_bar_5m:       str | None      # timestamp of the most recent 5m bar fetched
    portfolio_positions: list[dict]    # positions passed into this analysis (may be empty)


# ── Service ───────────────────────────────────────────────────────────────────

class TradeHelperService:

    def __init__(
        self,
        fmp_key:              str | None          = None,
        xai_key:              str | None          = None,
        llm_model:            str                 = "grok-3",
        atr_stop_mult:        float               = 2.0,
        atr_target_mult:      float               = 3.0,
        news_days:            int                 = 3,
        # account / sizing params
        equity:               float               = 100_000.0,
        risk_pct:             float               = 0.75,
        max_pos_pct:          float               = 10.0,
        max_adv_pct:          float               = 0.01,
        max_sector_pct:       float               = 30.0,
        book_sector_exposure: dict[str, float] | None = None,
    ) -> None:
        self._fmp_key        = fmp_key       or os.environ.get("FMP_API_KEY", "")
        self._xai_key        = xai_key or os.environ.get("XAI_API_KEY", "")
        self._llm_model      = llm_model
        self._atr_stop_mult  = atr_stop_mult
        self._atr_target_mult = atr_target_mult
        self._news_days      = news_days
        self._equity         = equity
        self._risk_pct       = risk_pct
        self._max_pos_pct    = max_pos_pct
        self._max_adv_pct    = max_adv_pct
        self._max_sector_pct = max_sector_pct
        self._book_sectors   = book_sector_exposure or {}

        self._cache        = MemoryCache()
        self._fundamentals = FundamentalsService(api_key=self._fmp_key) if self._fmp_key else None
        self._news_svc     = NewsService(lookback_days=news_days)

    @classmethod
    def from_config_yaml(cls, path: str = "config.yaml", **overrides) -> "TradeHelperService":
        """Build service from the trading engine config.yaml, if available."""
        try:
            import yaml
            from pathlib import Path
            with open(Path(path)) as f:
                cfg = yaml.safe_load(f)
            acct = cfg.get("account", {})
            rk   = cfg.get("risk",    {})
            keys = cfg.get("api_keys", {})
            # Build book sector exposure from portfolio entries
            book_sectors: dict[str, float] = {}
            for p in cfg.get("portfolio", []):
                sector = p.get("sector", "Unknown")
                book_sectors[sector] = book_sectors.get(sector, 0.0) + p.get("qty", 0) * p.get("entry", 0)
            kwargs = dict(
                fmp_key         = keys.get("fmp"),
                xai_key         = keys.get("xai") or os.environ.get("XAI_API_KEY"),
                equity          = float(acct.get("equity",            100_000)),
                risk_pct        = float(rk.get("risk_per_trade_pct",  0.75)),
                max_pos_pct     = float(rk.get("max_position_pct",    10.0)),
                max_adv_pct     = float(rk.get("max_adv_participation", 0.01)),
                max_sector_pct  = float(rk.get("max_sector_exposure_pct", 30.0)),
                atr_stop_mult   = float(rk.get("atr_stop_mult",       2.0)),
                atr_target_mult = float(rk.get("atr_target_mult",     3.0)),
                book_sector_exposure = book_sectors,
            )
            kwargs.update(overrides)
            return cls(**kwargs)
        except Exception as e:
            logger.warning("[TradeHelper] Could not load config.yaml (%s) — using defaults", e)
            return cls(**overrides)

    # ── Market context (SPY / QQQ / VIX) ─────────────────────────────────────

    async def _market_context(self) -> MarketContext:
        cached = self._cache.load("global", category="mkt_ctx")
        if cached is not None:
            return cached

        if not self._fundamentals:
            raise RuntimeError("FMP_API_KEY required for market context — set FMP_API_KEY env var")

        _FALLBACK = MarketContext(
            spy_price=0, spy_ret_1d=0, spy_ret_5d=0, spy_ret_20d=0,
            spy_above_200d=True, vix=20.0, vix_regime="low",
            qqq_ret_1d=0, qqq_ret_5d=0,
        )
        try:
            fmp = self._fundamentals
            spy_df, qqq_df = await asyncio.gather(
                fmp.get_ohlcv(_SPY, "1d", 220),
                fmp.get_ohlcv(_QQQ, "1d", 30),
            )
            try:
                vix_df  = await fmp.get_ohlcv(_VIX, "1d", 5)
                vix_val = float(vix_df["c"].iloc[-1])
            except Exception:
                vix_val = 20.0

            def _ret(df: pd.DataFrame, n: int) -> float:
                c = df["c"]
                return float(c.iloc[-1] / c.iloc[-n - 1] - 1) if len(c) > n else 0.0

            spy_close = float(spy_df["c"].iloc[-1])
            spy_200   = float(spy_df["c"].rolling(200).mean().iloc[-1]) if len(spy_df) >= 200 else 0.0

            result = MarketContext(
                spy_price      = spy_close,
                spy_ret_1d     = _ret(spy_df, 1),
                spy_ret_5d     = _ret(spy_df, 5),
                spy_ret_20d    = _ret(spy_df, 20),
                spy_above_200d = spy_close > spy_200,
                vix            = vix_val,
                vix_regime     = _vix_regime(vix_val),
                qqq_ret_1d     = _ret(qqq_df, 1),
                qqq_ret_5d     = _ret(qqq_df, 5),
            )
        except Exception as e:
            logger.warning("[TradeHelper] Market context fetch failed: %s", e)
            result = _FALLBACK

        self._cache.save("global", result, category="mkt_ctx", metadata={"ttl": _TTL_MKT_CTX})
        return result

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    async def _get_ohlcv(self, symbol: str, limit: int = 120) -> pd.DataFrame:
        if not self._fundamentals:
            raise RuntimeError("FMP_API_KEY required — set FMP_API_KEY env var")
        cache_key = f"{symbol}:{limit}"
        cached = self._cache.load(cache_key, category="ohlcv")
        if cached is not None:
            return cached
        df = await self._fundamentals.get_ohlcv(symbol, timeframe="1d", limit=limit)
        self._cache.save(cache_key, df, category="ohlcv", metadata={"ttl": _TTL_OHLCV})
        return df

    # ── Intraday SuperTrend (1m / 5m / 15m) ──────────────────────────────────

    async def _fetch_intraday_st_tf(self, symbol: str, tf: str) -> dict:
        if not self._fundamentals:
            raise RuntimeError("FMP_API_KEY required for intraday data")
        cache_key = f"{symbol}:{tf}"
        cached = self._cache.load(cache_key, category="intraday_st")
        if cached is not None:
            return cached

        try:
            df = await self._fundamentals.get_ohlcv(
                symbol, timeframe=tf, limit=_FMP_ST_LIMIT[tf], extended=True
            )

            st    = ind.supertrend(df, length=14, multiplier=2.5)
            price = float(df["c"].iloc[-1])
            st_val = float(st["value"].iloc[-1])
            result = {
                "value":     round(st_val, 4),
                "direction": int(st["direction"].iloc[-1]),
                "flipped":   bool(st["flipped"].iloc[-1]),
                "dist_pct":  round((price - st_val) / price * 100, 2),
            }
        except Exception as e:
            logger.warning("[TradeHelper] Intraday ST %s %s failed: %s", symbol, tf, e)
            result = {}

        self._cache.save(cache_key, result, category="intraday_st",
                         metadata={"ttl": _TTL_INTRADAY[tf]})
        return result

    async def _fetch_intraday_st(self, symbol: str) -> dict[str, dict]:
        tasks   = [asyncio.create_task(self._fetch_intraday_st_tf(symbol, tf))
                   for tf in _INTRADAY_TFS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            tf: (r if isinstance(r, dict) else {})
            for tf, r in zip(_INTRADAY_TFS, results)
        }

    # ── Intraday OHLCV bar CSVs (5m + 1h) for LLM prompt ────────────────────

    async def _fetch_bars(self, symbol: str, tf: str, n: int = 20) -> tuple[str, pd.DataFrame]:
        if not self._fundamentals:
            return "", pd.DataFrame()
        cache_key = f"{symbol}:{tf}:bars"
        cached = self._cache.load(cache_key, category="bars")
        if cached is not None:
            return cached
        try:
            limit   = _FMP_BARS_LIMIT.get(tf, n * 3)
            df      = await self._fundamentals.get_ohlcv(symbol, timeframe=tf, limit=limit, extended=True)
            csv_str = _ohlcv_to_csv(df, n)
            ttl     = _TTL_BARS.get(tf, 300)
            self._cache.save(cache_key, (csv_str, df), category="bars", metadata={"ttl": ttl})
            return csv_str, df
        except Exception as e:
            logger.warning("[TradeHelper] Bars fetch %s %s failed: %s", symbol, tf, e)
            return "", pd.DataFrame()

    async def _fetch_intraday_bars(self, symbol: str) -> dict:
        (csv_5m, df_5m), (csv_1h, _) = await asyncio.gather(
            self._fetch_bars(symbol, "5m", 20),
            self._fetch_bars(symbol, "1h", 20),
        )
        return {"5m_csv": csv_5m, "1h_csv": csv_1h, "5m_df": df_5m}

    # ── Portfolio correlations ────────────────────────────────────────────────

    async def _compute_portfolio_corrs(
        self,
        cand_ohlcv: pd.DataFrame,
        positions:  list[dict],
    ) -> dict[str, float]:
        """60d daily return correlation between candidate and each open position."""
        if not positions:
            return {}
        cand_ret = cand_ohlcv["c"].pct_change().dropna().tail(60)
        if len(cand_ret) < 20:
            return {}

        async def _resolve_ticker(sym: str, isin: str) -> str:
            """Return the best FMP ticker for a position.

            If sym looks like a company name (contains space, or >6 chars with no
            digits) and we have an ISIN, try ISIN→ticker resolution first.
            """
            if not self._fundamentals:
                return sym
            looks_like_name = " " in sym or (len(sym) > 6 and not any(c.isdigit() for c in sym))
            if looks_like_name and isin:
                ticker = await self._fundamentals.get_ticker_from_isin(isin, name_hint=sym)
                if ticker:
                    return ticker
            return sym

        async def _corr_one(pos: dict) -> tuple[str, float | None]:
            sym  = pos["symbol"]
            isin = pos.get("isin", "")
            cache_key = f"corr:{isin or sym}"
            cached = self._cache.load(cache_key, category="corr")
            if cached is not None:
                return sym, cached
            try:
                ticker  = await _resolve_ticker(sym, isin)
                df      = await self._get_ohlcv(ticker, 70)
                pos_ret = df["c"].pct_change().dropna().tail(60)
                n = min(len(cand_ret), len(pos_ret))
                if n < 20:
                    return sym, None
                corr = float(cand_ret.iloc[-n:].corr(pos_ret.iloc[-n:]))
                self._cache.save(cache_key, corr, category="corr", metadata={"ttl": _TTL_CORR})
                return sym, corr
            except Exception:
                return sym, None

        results = await asyncio.gather(*[asyncio.create_task(_corr_one(p)) for p in positions])
        return {sym: corr for sym, corr in results if corr is not None}

    # ── Technical indicators ──────────────────────────────────────────────────

    def _compute_technicals(self, df: pd.DataFrame) -> dict:
        result: dict = {}
        try:
            atr_s = ind.atr(df, length=14)
            px    = float(df["c"].iloc[-1])
            atr_v = float(atr_s.iloc[-1])

            result["price"]   = round(px, 4)
            result["atr"]     = round(atr_v, 4)
            result["atr_pct"] = round(atr_v / px * 100, 2) if px else 0.0

            rsi_s = ind.rsi(df, length=14)
            result["rsi"] = round(float(rsi_s.iloc[-1]), 1)

            adx_s = ind.adx(df, length=14)
            result["adx"] = round(float(adx_s.iloc[-1]), 1)

            result["ema8"]  = round(float(ind.ema(df, length=8).iloc[-1]),  2)
            result["ema20"] = round(float(ind.ema(df, length=20).iloc[-1]), 2)
            result["ema50"] = round(float(ind.ema(df, length=50).iloc[-1]), 2)

            st = ind.supertrend(df, length=14, multiplier=2.5)
            result["st_value"]   = round(float(st["value"].iloc[-1]),     2)
            result["st_dir"]     = int(st["direction"].iloc[-1])
            result["st_flipped"] = bool(st["flipped"].iloc[-1])

            close = df["c"]

            def _ret(n: int) -> float:
                return round(float(close.iloc[-1] / close.iloc[-n - 1] - 1) * 100, 2) if len(close) > n else 0.0

            result["ret_1d"]  = _ret(1)
            result["ret_5d"]  = _ret(5)
            result["ret_20d"] = _ret(20)
            result["ret_50d"] = _ret(50)

            adv20 = float(df["v"].rolling(20).mean().iloc[-1])
            result["adv20"]   = adv20
            result["rel_vol"] = round(float(df["v"].iloc[-1]) / adv20, 2) if adv20 else 1.0

        except Exception as e:
            logger.warning("[TradeHelper] Technicals failed: %s", e)
        return result

    # ── News + hard events ────────────────────────────────────────────────────

    def _get_news_sync(self, symbol: str) -> tuple[list[dict], list[dict], list[str]]:
        """Returns (hard_events, news_items, headline_strings).
        news_items: [{title, ts, source}] for display.
        headline_strings: plain titles for the LLM prompt.
        """
        cached = self._cache.load(symbol, category="news")
        if cached is not None:
            return cached
        try:
            raw = self._news_svc.get_news(symbol, lookback_days=self._news_days)
        except Exception:
            raw = []

        news_items = [
            {
                "title":  n.title,
                "ts":     n.published_date.strftime("%m/%d %H:%M"),
                "source": n.publisher or n.site or "",
            }
            for n in raw[:20]
        ]
        headlines = [n["title"] for n in news_items[:12]]

        seen: dict[str, dict] = {}
        for item in raw:
            text = f"{item.title} {getattr(item, 'summary', '')}".lower()
            for pattern, (label, score) in _HARD_EVENT_PATTERNS.items():
                if re.search(pattern, text, re.I):
                    if label not in seen or abs(score) > abs(seen[label]["score"]):
                        seen[label] = {"event": label, "score": score,
                                       "headline": item.title[:120]}
                    break

        result = list(seen.values()), news_items, headlines
        self._cache.save(symbol, result, category="news", metadata={"ttl": _TTL_NEWS})
        return result

    # ── Earnings ──────────────────────────────────────────────────────────────

    def _earnings_days_sync(self, symbol: str) -> int | None:
        cached = self._cache.load(symbol, category="earnings")
        if cached is not None:
            return cached  # may be the sentinel -1 meaning "checked, not found"
        try:
            import requests
            key = os.environ.get("FINNHUB_API_KEY", "")
            if not key:
                return None
            r = requests.get(
                "https://finnhub.io/api/v1/calendar/earnings",
                params={
                    "symbol": symbol,
                    "from": dt.date.today().isoformat(),
                    "to":   (dt.date.today() + dt.timedelta(days=30)).isoformat(),
                    "token": key,
                },
                timeout=10,
            )
            cal = r.json().get("earningsCalendar", [])
            if cal:
                days = (dt.date.fromisoformat(cal[0]["date"]) - dt.date.today()).days
                self._cache.save(symbol, days, category="earnings", metadata={"ttl": _TTL_EARNINGS})
                return days
        except Exception:
            pass
        # Cache the miss too so we don't hammer Finnhub on every call
        self._cache.save(symbol, None, category="earnings", metadata={"ttl": _TTL_EARNINGS})
        return None

    # ── Macro economic calendar ───────────────────────────────────────────────

    def _get_econ_calendar_sync(self) -> str:
        """Return today+5d US macro calendar as compact text. Cached 4 hours."""
        cached = self._cache.load("__global__", category="econ_calendar")
        if cached is not None:
            return cached
        try:
            import requests
            today = dt.date.today()
            to    = today + dt.timedelta(days=5)
            r = requests.get(
                "https://financialmodelingprep.com/api/v3/economic_calendar",
                params={
                    "from":   today.isoformat(),
                    "to":     to.isoformat(),
                    "apikey": self._fmp_key,
                },
                timeout=10,
            )
            events = [e for e in r.json() if e.get("country") == "US"]
            lines = []
            for e in events:
                date_str = e.get("date", "")[:16]   # "YYYY-MM-DD HH:MM"
                try:
                    ts = dt.datetime.strptime(date_str, "%Y-%m-%d %H:%M")
                    label = ts.strftime("%m/%d %H:%M")
                except ValueError:
                    label = date_str
                impact  = (e.get("impact") or "").capitalize()
                name    = e.get("event", "")
                actual  = e.get("actual")
                est     = e.get("estimate")
                prev    = e.get("previous")
                parts = []
                if actual is not None:
                    parts.append(f"actual={actual}")
                if est is not None:
                    parts.append(f"est={est}")
                if prev is not None:
                    parts.append(f"prev={prev}")
                detail = ", ".join(parts) if parts else "pending"
                lines.append(f"{label} [{impact}] {name}: {detail}")
            result = "\n".join(lines) if lines else "(none)"
        except Exception as exc:
            logger.warning("[TradeHelper] econ calendar fetch failed: %s", exc)
            result = "(unavailable)"
        self._cache.save("__global__", result, category="econ_calendar",
                         metadata={"ttl": _TTL_ECON_CAL})
        return result

    # ── Position sizing + sector concentration ───────────────────────────────

    def _compute_sizing(
        self,
        price:     float,
        atr:       float,
        adv20:     float,
        sector:    str,
        modifiers: list[float],
    ) -> dict:
        stop_dist = atr * self._atr_stop_mult
        if stop_dist <= 0 or price <= 0:
            return {"shares": 0, "reason": "invalid ATR/price"}

        risk_dollars    = self._equity * self._risk_pct / 100
        raw             = risk_dollars / stop_dist
        cap_notional    = (self._equity * self._max_pos_pct / 100) / price
        cap_liquidity   = adv20 * self._max_adv_pct if adv20 else raw

        shares = min(raw, cap_notional, cap_liquidity)
        for m in modifiers:
            shares *= m
        shares = int(shares)

        binding = min(
            [("risk", raw), ("notional", cap_notional), ("liquidity", cap_liquidity)],
            key=lambda x: x[1],
        )[0]

        notional = round(shares * price, 2)

        # Sector concentration gate
        cur_sector = self._book_sectors.get(sector, 0.0)
        new_pct    = (cur_sector + notional) / self._equity * 100 if self._equity else 0.0
        sector_ok  = new_pct <= self._max_sector_pct

        return {
            "shares":           shares,
            "risk_dollars":     round(shares * stop_dist, 2),
            "notional":         notional,
            "stop_distance":    round(stop_dist, 4),
            "binding":          binding,
            "sector":           sector,
            "sector_pct":       round(new_pct, 1),
            "sector_max_pct":   self._max_sector_pct,
            "sector_ok":        sector_ok,
            "equity":           self._equity,
        }

    # ── LLM synthesis ─────────────────────────────────────────────────────────

    async def _llm_analyse(
        self,
        symbol:        str,
        company_name:  str,
        sector:        str,
        market_cap:    float | None,
        beta:          float | None,
        description:   str,
        tech:          dict,
        intraday_st:   dict[str, dict],
        mkt:           MarketContext,
        events:        list[dict],
        headlines:     list[str],
        earnings_days: int | None,
        bars_5m:            str        = "",
        bars_1h:            str        = "",
        vol_1:              int        = 0,
        vol_2:              int        = 0,
        session_ratio:      str        = "—",
        portfolio_positions: list[dict] | None = None,
        portfolio_corrs:    dict[str, float] | None = None,
        econ_calendar:      str        = "(unavailable)",
    ) -> dict:
        """Returns a dict matching the LLM output schema (all keys always present)."""
        if not self._xai_key:
            logger.warning("[TradeHelper] XAI_API_KEY is empty — falling back to rule-based verdict")
            print("  ⚠️  [TradeHelper] XAI_API_KEY not set — using rule-based fallback")
            return self._rule_based_verdict(tech, mkt, events)

        try:
            from infrastructure.gateways.llms.grok_client import GrokLLM
            from core.adapters.llm import LLMRequest
            from services.llm_prompt import SYSTEM_PROMPT, build_user_prompt, parse_card, validate_card
            import zoneinfo

            logger.info("[TradeHelper] Calling Grok (%s) for %s", self._llm_model, symbol)
            client = GrokLLM(api_key=self._xai_key)

            # ── NOW context ───────────────────────────────────────────────────
            et     = zoneinfo.ZoneInfo("America/New_York")
            now_et = dt.datetime.now(et)
            mins   = now_et.hour * 60 + now_et.minute
            if   mins < 9*60+30: phase = "pre-market"
            elif mins < 10*60:   phase = "first 30min"
            elif mins < 15*60:   phase = "mid-session"
            elif mins < 16*60:   phase = "final hour"
            else:                phase = "after-hours"
            last_bar_time = bars_5m.strip().splitlines()[-1].split(",")[0] if bars_5m.strip() else "—"

            # ── Formatted fields ──────────────────────────────────────────────
            blackout = "  ⚠ BLACKOUT RISK" if earnings_days is not None and earnings_days <= 3 else ""

            ctx = {
                "symbol":           symbol,
                "company":          company_name,
                "sector":           sector,
                "mkt_cap":          f"${market_cap/1e9:.1f}B" if market_cap else "N/A",
                "beta":             f"{beta:.2f}" if beta is not None else "N/A",
                "description_300":  (description[:300] + "…") if len(description) > 300 else description,
                "timestamp":        now_et.strftime("%Y-%m-%d %H:%M"),
                "weekday":          now_et.strftime("%A"),
                "session_phase":    phase,
                "last_bar_time":    last_bar_time,
                "price":            f"{tech.get('price', 0):.2f}",
                "atr":              f"{tech.get('atr', 0):.2f}",
                "atr_pct":          f"{tech.get('atr_pct', 0):.2f}",
                "rsi":              f"{tech.get('rsi', 0):.1f}",
                "adx":              f"{tech.get('adx', 0):.1f}",
                "trend_label":      "trending" if tech.get("adx", 0) > 25 else "ranging",
                "st_dir":           "LONG" if tech.get("st_dir") == 1 else "SHORT",
                "st_value":         f"{tech.get('st_value', 0):.2f}",
                "st_flip":          "yes" if tech.get("st_flipped") else "no",
                "ema8":             f"{tech.get('ema8', 0):.2f}",
                "ema20":            f"{tech.get('ema20', 0):.2f}",
                "ema50":            f"{tech.get('ema50', 0):.2f}",
                "r1d":              f"{tech.get('ret_1d', 0):+.2f}",
                "r5d":              f"{tech.get('ret_5d', 0):+.2f}",
                "r20d":             f"{tech.get('ret_20d', 0):+.2f}",
                "r50d":             f"{tech.get('ret_50d', 0):+.2f}",
                "rel_vol":          f"{tech.get('rel_vol', 1):.2f}",
                "intraday_st":      _format_st_compact(intraday_st, tech),
                "bars_5m_csv":      bars_5m or "(unavailable)",
                "bars_1h_csv":      bars_1h or "(unavailable)",
                "vol_cur":          f"{vol_1:,}",
                "vol_prev":         f"{vol_2:,}",
                "session_vol_ratio": session_ratio,
                "spy_px":           f"{mkt.spy_price:.2f}",
                "spy_r1d":          f"{mkt.spy_ret_1d*100:+.2f}",
                "spy_r5d":          f"{mkt.spy_ret_5d*100:+.2f}",
                "spy_r20d":         f"{mkt.spy_ret_20d*100:+.2f}",
                "spy_ma200":        "ABOVE" if mkt.spy_above_200d else "BELOW",
                "qqq_r1d":          f"{mkt.qqq_ret_1d*100:+.2f}",
                "qqq_r5d":          f"{mkt.qqq_ret_5d*100:+.2f}",
                "vix":              f"{mkt.vix:.1f}",
                "vix_regime":       mkt.vix_regime,
                "days_to_earnings": str(earnings_days) if earnings_days is not None else ">30",
                "blackout_flag":    blackout,
                "econ_calendar":    econ_calendar,
                "hard_events":      (
                    "\n".join(f"[{e['event'].upper()}] score={e['score']:+.1f}  {e['headline']}"
                              for e in events)
                    if events else "none"
                ),
                "headlines": (
                    "\n".join(f"- {h}" for h in headlines[:12])
                    if headlines else "(none)"
                ),
            }

            # ── Portfolio section ─────────────────────────────────────────────
            from services.llm_prompt import format_portfolio_lines
            p_lines, avg_corr, net_bias = format_portfolio_lines(
                portfolio_positions or [], portfolio_corrs or {}
            )
            ctx["portfolio_lines"] = p_lines
            ctx["avg_corr"]        = avg_corr
            ctx["net_bias"]        = net_bias

            req = LLMRequest(
                prompt=build_user_prompt(ctx),
                system=SYSTEM_PROMPT,
                model=self._llm_model,
                max_tokens=1_200,
                temperature=0.3,
            )
            resp = await client.complete(req)
            card = parse_card(resp.text)
            card = validate_card(card, float(ctx["price"]), float(ctx["atr"]))

            return {
                "verdict":           card.get("verdict",           "WATCH"),
                "side_bias":         card.get("side_bias",         "neutral"),
                "confidence":        card.get("confidence",        "low"),
                "timeframe":         card.get("timeframe",         None),
                "sentiment":         card.get("sentiment",         None),
                "level_read":        card.get("level_read",        None),
                "entry":             card.get("entry",             None),
                "stop":              card.get("stop",              None),
                "targets":           card.get("targets",           []),
                "risk_reward":       card.get("risk_reward",       None),
                "bull_case":         card.get("bull_case",         []),
                "bear_case":         card.get("bear_case",         []),
                "key_risks":         [],
                "watch_for":         card.get("watch_for",         None),
                "portfolio_fit":     card.get("portfolio_fit",     None),
                "hold_plan":         card.get("hold_plan",         None),
                "validation_issues": card.get("validation_issues", []),
                "synthesis":         card.get("synthesis",         ""),
            }

        except Exception as e:
            err_str = str(e)
            if "403" in err_str or "permission-denied" in err_str or "permission_denied" in err_str:
                print(
                    "  ⚠️  [TradeHelper] Grok API blocked (403 – no credits on xAI account).\n"
                    "       Add credits at: https://console.x.ai  then try again."
                )
                logger.warning("[TradeHelper] Grok 403 permission-denied — account has no credits")
            else:
                import traceback
                logger.warning("[TradeHelper] LLM analysis failed (%s), falling back to rules", e)
                print(f"  ⚠️  [TradeHelper] Grok API call failed: {e}")
                traceback.print_exc()
            return self._rule_based_verdict(tech, mkt, events)

    @staticmethod
    def _rule_based_verdict(
        tech: dict, mkt: MarketContext, events: list[dict],
    ) -> dict:
        """Fallback when LLM is unavailable. Returns same dict shape as _llm_analyse."""
        bull: list[str] = []
        bear: list[str] = []
        risks: list[str] = []

        _BLOCK_BOTH = {"bankruptcy", "accounting"}
        blocking = [e for e in events if e["event"] in _BLOCK_BOTH]
        if blocking:
            return {
                "verdict": "AVOID", "side_bias": "neutral", "confidence": "high",
                "timeframe": None, "sentiment": None, "entry": None, "stop": None,
                "targets": [], "risk_reward": None, "watch_for": None,
                "bull_case": [],
                "bear_case":  [f"Structural event: {e['event']}" for e in blocking],
                "key_risks":  ["Stock may halt or be de-listed — no new entries"],
                "hold_plan":  None,
                "synthesis":  "Structural event (bankruptcy/going concern) present. Avoid all entries.",
            }

        pos_events = [e for e in events if e["score"] >= 0.6]
        neg_events = [e for e in events if e["score"] <= -0.7 and e["event"] not in _BLOCK_BOTH]

        score  = 0
        st_dir = tech.get("st_dir", 0)
        if st_dir == 1:
            score += 2
            bull.append(f"SuperTrend LONG ({tech.get('st_value', 0):.2f})")
        else:
            score -= 2
            bear.append(f"SuperTrend SHORT ({tech.get('st_value', 0):.2f})")

        if neg_events:
            score -= 1
            bear.append(f"Negative catalyst: {neg_events[0]['event']} — supports short")
        if pos_events:
            score += 1
            bull.append(f"Positive catalyst: {pos_events[0]['event']} — supports long")

        rsi_v = tech.get("rsi", 50.0)
        if rsi_v < 30:
            score += 1
            bull.append(f"RSI oversold at {rsi_v:.0f} — mean-reversion opportunity")
        elif rsi_v > 70:
            score -= 1
            bear.append(f"RSI overbought at {rsi_v:.0f} — momentum stretched")

        if tech.get("ema8", 0) > tech.get("ema20", 0):
            score += 1
            bull.append("EMA8 > EMA20 — short-term uptrend intact")
        else:
            score -= 1
            bear.append("EMA8 < EMA20 — short-term downtrend")

        if mkt.spy_above_200d:
            score += 1
            bull.append("SPY above 200d MA — macro bull regime")
        else:
            score -= 1
            bear.append("SPY below 200d MA — macro tailwind for shorts")

        if mkt.vix > 30:
            score -= 1
            risks.append(f"VIX at {mkt.vix:.1f} — elevated volatility, widen stops")

        if score >= 3:
            verdict, side, conf = "ENTER", "long",  "medium"
        elif score <= -3:
            verdict, side, conf = "ENTER", "short", "medium"
        else:
            verdict, side, conf = "WATCH", "neutral", "low"

        synth = (
            f"Rule-based analysis (LLM unavailable). Score {score:+d}/5. "
            f"SuperTrend {'long' if st_dir == 1 else 'short'}, "
            f"RSI {rsi_v:.0f}, VIX {mkt.vix:.1f}."
        )
        return {
            "verdict": verdict, "side_bias": side, "confidence": conf,
            "timeframe": None, "sentiment": None, "level_read": None,
            "entry": None, "stop": None, "targets": [], "risk_reward": None,
            "watch_for": None, "portfolio_fit": None, "hold_plan": None,
            "validation_issues": [],
            "bull_case": bull, "bear_case": bear, "key_risks": risks,
            "synthesis": synth,
        }

    # ── Main entry point ──────────────────────────────────────────────────────

    async def analyse(
        self,
        symbol:            str,
        current_positions: list[dict] | None = None,
    ) -> TradeHelperReport:
        symbol = symbol.upper()
        now    = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

        cached_report = self._cache.load(symbol, category="report")
        if cached_report is not None:
            logger.debug("[TradeHelper] %s: returning cached report", symbol)
            return cached_report

        # Kick off parallel fetches
        ohlcv_task    = asyncio.create_task(self._get_ohlcv(symbol))
        mkt_task      = asyncio.create_task(self._market_context())
        intraday_task = asyncio.create_task(self._fetch_intraday_st(symbol))
        bars_task     = asyncio.create_task(self._fetch_intraday_bars(symbol))
        profile_task  = (
            asyncio.create_task(self._fundamentals.get_profile(symbol))
            if self._fundamentals else None
        )

        ohlcv, mkt, intraday_st, bars_data = await asyncio.gather(
            ohlcv_task, mkt_task, intraday_task, bars_task
        )
        profile = await profile_task if profile_task else None

        company_name = (profile.company_name or symbol) if profile else symbol
        sector       = (profile.sector       or "Unknown") if profile else "Unknown"
        industry     = (profile.industry     or "")        if profile else ""
        market_cap   = getattr(profile, "market_cap",   None) if profile else None
        beta         = getattr(profile, "beta",         None) if profile else None
        description  = (profile.description  or "")     if profile else ""

        tech = self._compute_technicals(ohlcv)

        # Portfolio correlations (runs after ohlcv is available)
        positions       = current_positions or []
        portfolio_corrs = await self._compute_portfolio_corrs(ohlcv, positions)

        # Volume profile from 5m bars
        df_5m          = bars_data.get("5m_df", pd.DataFrame())
        bars_5m_csv    = bars_data.get("5m_csv", "")
        bars_1h_csv    = bars_data.get("1h_csv", "")
        vol_1          = int(df_5m["v"].iloc[-1]) if not df_5m.empty and len(df_5m) >= 1 else 0
        vol_2          = int(df_5m["v"].iloc[-2]) if not df_5m.empty and len(df_5m) >= 2 else 0
        session_ratio  = _session_vol_ratio(df_5m, tech.get("adv20", 0.0))
        last_bar_5m    = str(df_5m["t"].iloc[-1]) if not df_5m.empty else None

        loop = asyncio.get_event_loop()
        (events, news_items, headlines), earnings_days, econ_calendar = await asyncio.gather(
            loop.run_in_executor(None, self._get_news_sync, symbol),
            loop.run_in_executor(None, self._earnings_days_sync, symbol),
            loop.run_in_executor(None, self._get_econ_calendar_sync),
        )

        llm = await self._llm_analyse(
            symbol, company_name, sector, market_cap, beta, description,
            tech, intraday_st, mkt, events, headlines, earnings_days,
            bars_5m=bars_5m_csv, bars_1h=bars_1h_csv,
            vol_1=vol_1, vol_2=vol_2, session_ratio=session_ratio,
            portfolio_positions=positions, portfolio_corrs=portfolio_corrs,
            econ_calendar=econ_calendar,
        )

        verdict    = llm["verdict"]
        side_bias  = llm["side_bias"]
        confidence = llm["confidence"]
        bull       = llm["bull_case"]
        bear       = llm["bear_case"]
        risks      = llm.get("key_risks", [])
        synthesis  = llm["synthesis"]
        timeframe        = llm.get("timeframe")
        sentiment        = llm.get("sentiment")
        watch_for        = llm.get("watch_for")
        hold_plan        = llm.get("hold_plan")
        level_read       = llm.get("level_read")
        portfolio_fit    = llm.get("portfolio_fit")
        validation_issues = llm.get("validation_issues", [])
        targets_list = llm.get("targets", [])
        llm_entry  = llm.get("entry")
        llm_stop   = llm.get("stop")
        llm_rr     = llm.get("risk_reward")

        price = tech.get("price", 0.0)
        atr_v = tech.get("atr",   0.0)

        if verdict == "ENTER" and side_bias in ("long", "short") and price:
            # Prefer LLM-provided structure-based levels; ATR fallback only when absent
            sign = 1 if side_bias == "long" else -1
            if llm_stop and llm_stop.get("level"):
                stop       = round(float(llm_stop["level"]), 2)
                stop_basis = llm_stop.get("basis", "")
            elif atr_v:
                stop       = round(price - sign * atr_v * self._atr_stop_mult, 2)
                stop_basis = f"ATR×{self._atr_stop_mult}"
            else:
                stop = stop_basis = None

            if targets_list:
                target = round(float(targets_list[0]["level"]), 2)
            elif atr_v:
                target = round(price + sign * atr_v * self._atr_target_mult, 2)
            else:
                target = None

            rr     = round(float(llm_rr), 2) if llm_rr else (
                round(self._atr_target_mult / self._atr_stop_mult, 1) if atr_v else None
            )
            sizing = self._compute_sizing(price, atr_v, tech.get("adv20", 0.0), sector, [])
            entry_ref = float(llm_entry["zone_low"]) if llm_entry else price
        else:
            stop = stop_basis = target = rr = sizing = entry_ref = None

        report = TradeHelperReport(
            symbol       = symbol,
            timestamp    = now,
            verdict      = verdict,
            side_bias    = side_bias,
            confidence   = confidence,
            company_name = company_name,
            sector       = sector,
            industry     = industry,
            market_cap   = market_cap,
            beta         = beta,
            description  = description,
            price        = price,
            atr          = atr_v,
            atr_pct      = tech.get("atr_pct", 0.0),
            technicals   = tech,
            intraday_st  = intraday_st,
            market_context = mkt,
            news_events  = events,
            news_items   = news_items,
            earnings_days = earnings_days,
            bull_case    = bull,
            bear_case    = bear,
            key_risks    = risks,
            llm_synthesis = synthesis,
            entry_ref    = entry_ref,
            stop         = stop,
            stop_basis   = stop_basis,
            target       = target,
            targets_list = targets_list,
            rr           = rr,
            sizing       = sizing,
            timeframe    = timeframe,
            sentiment    = sentiment,
            entry_zone        = llm_entry,
            watch_for         = watch_for,
            portfolio_fit     = portfolio_fit,
            hold_plan         = hold_plan,
            level_read        = level_read,
            validation_issues = validation_issues,
            last_bar_5m       = last_bar_5m,
            portfolio_positions = positions,
        )
        self._cache.save(symbol, report, category="report", metadata={"ttl": _TTL_REPORT})
        return report
