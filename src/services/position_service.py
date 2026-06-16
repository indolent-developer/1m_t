"""
services.position_service

Shared position-resolution helpers used by both the CLI and Telegram interfaces.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DATA = Path(__file__).resolve().parents[2] / "data"


async def find_position(broker, ticker: str):
    """
    Find an open position by FMP ticker symbol.

    Scalable Capital often holds a stock under a different ISIN than FMP uses
    (e.g. EU listing US64110L1061 vs US NASDAQ listing US64110W1027 for NFLX).
    Four-step resolution:
      1. Direct symbol match (fast path for brokers that store tickers)
      2. Broker's own ISIN resolver → position ISIN match
      3. File cache ISIN match (FMP ISINs; may miss cross-listed stocks)
      4. FMP company name prefix match (robust final fallback)
    """
    # 1. Direct
    pos = await broker.get_position(ticker)
    if pos:
        return pos

    all_pos = await broker.get_positions()

    # 2. Broker ISIN (uses Scalable's own data — correct exchange listing)
    _resolve = getattr(broker, "_resolve_isin", None)
    if _resolve:
        try:
            broker_isin = await _resolve(ticker)
            for p in all_pos:
                if (p.id or "").upper() == broker_isin.upper():
                    return p
        except Exception:
            pass

    # 3. File cache ISIN (FMP may return a different exchange's ISIN)
    cache_file = _DATA / "isin_ticker_cache.json"
    if cache_file.exists():
        isin_map = json.loads(cache_file.read_text())
        fmp_isin = next(
            (k for k, t in isin_map.items() if t.upper() == ticker.upper()), None
        )
        if fmp_isin:
            for p in all_pos:
                if (p.id or "").upper() == fmp_isin.upper():
                    return p

    # 4. FMP company name → position name prefix match
    try:
        from services.fundamentals_service import FundamentalsService
        svc = FundamentalsService(api_key=os.environ.get("FMP_API_KEY", ""))
        profile = await svc.get_profile(ticker)
        if profile and profile.company_name:
            # "Netflix, Inc." → "NETFLIX"; "Alphabet Inc." → "ALPHABET"
            first_word = profile.company_name.split(",")[0].strip().split()[0].upper()
            for p in all_pos:
                if p.symbol.upper().startswith(first_word):
                    return p
    except Exception:
        pass

    return None


async def get_position_for_ticker(broker, ticker: str):
    """Return Position matching ticker, resolving via ISIN (Scalable stores company name, not ticker)."""
    _resolve = getattr(broker, "_resolve_isin", None)
    isin = None
    if _resolve:
        try:
            isin = (await _resolve(ticker)).upper()
        except Exception:
            pass

    positions = await broker.get_positions()
    if isin:
        for p in positions:
            if (p.id or "").upper() == isin:
                return p
    # Fallback: ticker substring in company name
    ticker_up = ticker.upper()
    for p in positions:
        if ticker_up in (p.symbol or "").upper():
            return p
    return None
