"""
services.fundamentals_service

Fetches company profile data (ISIN, sector, market cap, etc.) from the
Financial Modeling Prep stable API.

Cache strategy: profiles are stored in data/fundamentals_cache.json keyed
by uppercase symbol. Each entry records the fetch date; stale entries older
than CACHE_TTL_DAYS are re-fetched transparently.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from core.utils.log_helper import getLogger
import os
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd

from core.entities.company_profile import CompanyProfile

# FMP interval name → (endpoint path, is_intraday)
_TF_MAP: dict[str, tuple[str, bool]] = {
    "1m":  ("historical-chart/1min",  True),
    "5m":  ("historical-chart/5min",  True),
    "15m": ("historical-chart/15min", True),
    "30m": ("historical-chart/30min", True),
    "1h":  ("historical-chart/1hour", True),
    "4h":  ("historical-chart/4hour", True),
    "1d":  ("historical-price-eod/full", False),
}

logger = getLogger(__name__)

_STABLE_BASE       = "https://financialmodelingprep.com/stable"
_V3_BASE           = "https://financialmodelingprep.com/v3"
CACHE_TTL_DAYS     = 7
_CACHE_PATH        = Path(__file__).resolve().parents[2] / "data" / "fundamentals_cache.json"
_ISIN_CACHE_PATH   = Path(__file__).resolve().parents[2] / "data" / "isin_ticker_cache.json"

# Exchange preference order — lower index wins
_EXCHANGE_RANK: dict[str, int] = {
    "NASDAQ":   0,
    "NYSE":     1,
    "AMEX":     2,
    "NYSE ARCA": 3,
    "NYSEARCA": 3,
    "OTC":      4,
}

import re as _re
_STRIP_SUFFIX = _re.compile(
    r"\s+(?:Corp\.?|Inc\.?|Ltd\.?|PLC|Holdings?|Group|Class\s+[A-C]|[A-C])$",
    _re.IGNORECASE,
)


class FundamentalsService:
    """
    Company profile data with 7-day file-based cache.

    Usage:
        svc = FundamentalsService(api_key="...")
        profile = await svc.get_profile("AAPL")
        print(profile.isin)        # US0378331005
        print(profile.company_name)# Apple Inc.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FMP_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "FMP API key required — set FMP_API_KEY env var or pass api_key=..."
            )
        self._cache: dict = self._load_cache()
        self._http = httpx.AsyncClient(timeout=15.0)

    # ── Cache I/O ─────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        if _CACHE_PATH.exists():
            try:
                return json.loads(_CACHE_PATH.read_text())
            except Exception:
                logger.warning("[FundamentalsService] Could not read cache — starting fresh")
        return {}

    def _save_cache(self) -> None:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps(self._cache, indent=2))
        except Exception as e:
            logger.warning("[FundamentalsService] Could not write cache: %s", e)

    def _cached_profile(self, symbol: str) -> Optional[CompanyProfile]:
        entry = self._cache.get(symbol.upper())
        if not entry:
            return None
        try:
            cached_date = dt.date.fromisoformat(entry["date"])
            if (dt.date.today() - cached_date).days > CACHE_TTL_DAYS:
                return None
            return CompanyProfile.from_dict(entry["data"])
        except Exception:
            return None

    def _store_profile(self, symbol: str, raw: dict) -> None:
        self._cache[symbol.upper()] = {
            "date": dt.date.today().isoformat(),
            "data": raw,
        }
        self._save_cache()

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def _get(self, url: str, params: dict) -> list | dict | None:
        """GET helper — raises on HTTP error, returns parsed JSON or None."""
        try:
            resp = await self._http.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("[FundamentalsService] GET %s failed: %s", url, e)
            return None

    async def _fetch_profile(self, symbol: str) -> Optional[CompanyProfile]:
        data = await self._get(
            f"{_STABLE_BASE}/profile",
            {"symbol": symbol.upper(), "apikey": self._api_key},
        )
        if not data or not isinstance(data, list):
            return None
        raw = data[0]
        self._store_profile(symbol, raw)
        return CompanyProfile.from_dict(raw)

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_profile(self, symbol: str) -> Optional[CompanyProfile]:
        """Return CompanyProfile for symbol, using cache when fresh (≤7 days)."""
        cached = self._cached_profile(symbol)
        if cached is not None:
            return cached
        try:
            return await self._fetch_profile(symbol)
        except Exception as e:
            logger.warning("[FundamentalsService] Profile fetch failed for %s: %s", symbol, e)
            return None

    async def get_isin(self, symbol: str) -> Optional[str]:
        """Convenience method — returns just the ISIN, or None if not found."""
        profile = await self.get_profile(symbol)
        return profile.isin if profile and profile.isin else None

    def _ticker_from_cache(self, isin: str) -> Optional[str]:
        """Scan cached profiles for one whose isin matches."""
        isin_up = isin.upper()
        for ticker, entry in self._cache.items():
            cached_isin = (entry.get("data") or {}).get("isin") if isinstance(entry, dict) else None
            if cached_isin and cached_isin.upper() == isin_up:
                return ticker
        return None

    # ── ISIN→ticker persistent cache ──────────────────────────────────────────

    def _load_isin_cache(self) -> dict:
        if _ISIN_CACHE_PATH.exists():
            try:
                return json.loads(_ISIN_CACHE_PATH.read_text())
            except Exception:
                pass
        return {}

    def _save_isin_entry(self, isin: str, ticker: str) -> None:
        try:
            data = self._load_isin_cache()
            data[isin.upper()] = ticker
            _ISIN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _ISIN_CACHE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
        except Exception as e:
            logger.debug("[FundamentalsService] Could not save ISIN cache: %s", e)

    def _isin_from_file_cache(self, isin: str) -> Optional[str]:
        data = self._load_isin_cache()
        return data.get(isin.upper())

    # ── Name search ───────────────────────────────────────────────────────────

    @staticmethod
    def _best_ticker(rows: list) -> Optional[str]:
        """Pick the best US-exchange ticker from FMP search results."""
        candidates = []
        for row in rows:
            sym  = (row.get("symbol") or "").strip()
            exch = (row.get("exchangeShortName") or "").upper()
            name = (row.get("name") or "").lower()
            if not sym or "." in sym:                 # skip cross-listed variants
                continue
            if sym.endswith(("-WT", "-W")):            # warrants with hyphen
                continue
            if "warrant" in name or "wt exp" in name: # warrants by name
                continue
            rank = _EXCHANGE_RANK.get(exch, 99)
            candidates.append((rank, sym, exch))
        if not candidates:
            return None
        # Drop warrant-style symbols where the base (sym[:-1]) is also a candidate
        syms = {c[1] for c in candidates}
        candidates = [c for c in candidates
                      if not (c[1].endswith("W") and c[1][:-1] in syms)]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    async def _search_by_isin(self, isin: str) -> Optional[str]:
        """Direct ISIN lookup via FMP /stable/search-isin; rank by exchange quality."""
        rows = await self._get(
            f"{_STABLE_BASE}/search-isin",
            {"isin": isin, "apikey": self._api_key},
        )
        if rows and isinstance(rows, list):
            return self._best_ticker(rows)
        return None

    async def _search_by_name(self, name: str) -> Optional[str]:
        """Search FMP v3/search by company name; rank by exchange quality."""
        clean = _STRIP_SUFFIX.sub("", name).strip()
        for query in ([clean, name] if clean != name else [name]):
            rows = await self._get(
                f"{_V3_BASE}/search",
                {"query": query, "apikey": self._api_key, "limit": 10},
            )
            if rows and isinstance(rows, list):
                ticker = self._best_ticker(rows)
                if ticker:
                    return ticker
        return None

    async def get_ticker_from_isin(
        self,
        isin: str,
        name_hint: str = "",
        broker_id: str = "",
    ) -> Optional[str]:
        """
        Resolve ISIN → FMP ticker.
        Order:
          1. Broker-level ISIN override (ticker_isin.json [broker_id]._isin_override)
          2. Profile cache (already fetched via /ind)
          3. Persistent ISIN→ticker file cache (data/isin_ticker_cache.json)
          4. FMP /stable/search-isin  — direct, authoritative
          5. FMP v3/search by name    — fallback for non-US ISINs FMP may not index
        """
        # 1. Broker-level override (highest priority)
        if broker_id:
            from interfaces.console.cmd_sym import get_broker_isin_override
            ov = get_broker_isin_override(broker_id, isin)
            if ov:
                return ov

        # 2. Profile cache (already fetched via /ind)
        ticker = self._ticker_from_cache(isin)
        if ticker:
            return ticker

        # 3. Persistent ISIN→ticker file cache
        ticker = self._isin_from_file_cache(isin)
        if ticker:
            return ticker

        # 4. FMP direct ISIN search
        ticker = await self._search_by_isin(isin)
        if ticker:
            self._save_isin_entry(isin, ticker)
            return ticker

        # 5. FMP name search fallback (catches non-US ISINs like AU/CA listings)
        if name_hint:
            ticker = await self._search_by_name(name_hint)
            if ticker:
                self._save_isin_entry(isin, ticker)
                await self.get_profile(ticker)
                return ticker

        logger.debug("[FundamentalsService] Could not resolve ISIN %s (hint=%r)", isin, name_hint)
        return None

    # ── FX ────────────────────────────────────────────────────────────────────

    async def get_fx_rate(self, from_ccy: str, to_ccy: str) -> float:
        """Return live exchange rate from_ccy → to_ccy (e.g. USD → EUR)."""
        if from_ccy.upper() == to_ccy.upper():
            return 1.0
        try:
            resp = await self._http.get(
                "https://api.frankfurter.app/latest",
                params={"from": from_ccy.upper(), "to": to_ccy.upper()},
            )
            resp.raise_for_status()
            return float(resp.json()["rates"][to_ccy.upper()])
        except Exception as e:
            logger.warning("[FundamentalsService] FX fetch failed %s→%s: %s", from_ccy, to_ccy, e)
            return 0.0

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    async def get_ohlcv(
        self,
        symbol:    str,
        timeframe: str  = "1d",
        limit:     int  = 60,
        extended:  bool = False,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV bars from FMP as a DataFrame with columns t,o,h,l,c,v.

        timeframe: 1m | 5m | 15m | 30m | 1h | 4h | 1d
        limit:     number of bars to return (sorted ascending)
        extended:  include pre-market and after-hours bars (intraday only)
        """
        timeframe = timeframe.lower()
        if timeframe not in _TF_MAP:
            raise ValueError(f"Unknown timeframe '{timeframe}'. Use: {', '.join(_TF_MAP)}")

        endpoint, intraday = _TF_MAP[timeframe]
        params: dict = {"symbol": symbol.upper(), "apikey": self._api_key, "limit": limit}
        if intraday and extended:
            params["extended"] = "true"

        resp = await self._http.get(f"{_STABLE_BASE}/{endpoint}", params=params)
        resp.raise_for_status()
        rows = resp.json()
        if not rows or not isinstance(rows, list):
            raise RuntimeError(f"No OHLCV data for {symbol} ({timeframe})")

        df = pd.DataFrame(rows)
        df = df.rename(columns={"date": "t", "open": "o", "high": "h", "low": "l",
                                 "close": "c", "volume": "v"})
        df["t"] = pd.to_datetime(df["t"], utc=True)
        df = df[["t", "o", "h", "l", "c", "v"]].sort_values("t").reset_index(drop=True)
        # FMP ignores the limit param when extended=True — enforce it here
        if len(df) > limit:
            df = df.tail(limit).reset_index(drop=True)
        return df
