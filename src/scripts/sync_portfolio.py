"""
scripts.sync_portfolio

One-shot: connect to a broker, fetch open positions, resolve ISINs → tickers
via FMP (Scalable only), update ticker_isin.json and watched_symbols.json.

Usage:
    python src/scripts/sync_portfolio.py              # sync all brokers
    python src/scripts/sync_portfolio.py scalable     # sync one broker
    python src/scripts/sync_portfolio.py ibkr
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sync_portfolio")

_PERSIST_FILE     = Path(__file__).parents[2] / "data" / "watched_symbols.json"
_TICKER_ISIN_FILE = Path(__file__).parents[2] / "data" / "ticker_isin.json"
_FMP_BASE         = "https://financialmodelingprep.com/stable/search-isin"


# ── file helpers ──────────────────────────────────────────────────────────────

def _load_watched() -> dict:
    try:
        if _PERSIST_FILE.exists():
            raw = json.loads(_PERSIST_FILE.read_text())
            if raw and "portfolio" not in raw and "scanners" not in raw:
                return {"portfolio": {}, "scanners": raw}
            return raw
    except Exception as e:
        logger.warning("Could not read %s: %s", _PERSIST_FILE, e)
    return {"portfolio": {}, "scanners": {}}


def _load_ticker_isin() -> dict:
    try:
        if _TICKER_ISIN_FILE.exists():
            return json.loads(_TICKER_ISIN_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_ticker_isin(data: dict) -> None:
    _TICKER_ISIN_FILE.write_text(json.dumps(data, indent=2))
    logger.info("Updated %s", _TICKER_ISIN_FILE)


def _save_watched(data: dict) -> None:
    _PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PERSIST_FILE.write_text(json.dumps(data, indent=2))
    logger.info("Updated %s", _PERSIST_FILE)


def _isin_to_ticker_map(ticker_isin: dict, broker_id: str) -> dict[str, str]:
    section = ticker_isin.get(broker_id, {})
    return {isin.upper(): ticker for ticker, isin in section.items()}


# ── FMP resolver (Scalable only — IBKR already returns tickers) ───────────────

def _fmp_lookup(isin: str, api_key: str) -> str | None:
    import requests
    try:
        r = requests.get(_FMP_BASE, params={"isin": isin, "apikey": api_key}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        for item in data:
            exch = (item.get("exchangeShortName") or "").upper()
            if exch in ("NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "BATS"):
                return item.get("symbol")
        return data[0].get("symbol")
    except Exception as e:
        logger.warning("FMP lookup failed for %s: %s", isin, e)
        return None


async def _fmp_lookup_async(isin: str, api_key: str) -> str | None:
    return await asyncio.to_thread(_fmp_lookup, isin, api_key)


# ── per-broker sync ───────────────────────────────────────────────────────────

async def sync_broker(
    broker_id: str,
    cfg_name: str,
    BrokerCls,
    fmp_key: str,
    ticker_isin: dict,
    resolve_isin: bool,
    cfg_patch: dict = {},
) -> tuple[list[str], object]:
    from core.config.config_loader import ConfigLoader

    cfg = ConfigLoader().load_broker(cfg_name)
    for k, v in cfg_patch.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    broker = BrokerCls(cfg)
    connected = await broker.connect()
    if not connected:
        logger.warning("[%s] connect() returned False — skipping", broker_id)
        return [], broker

    positions = await broker.get_positions()
    tickers   = []

    if not resolve_isin:
        seen = set()
        for p in positions:
            ticker = (p.symbol or "").strip().upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                logger.info("  %s", ticker)
                tickers.append(ticker)
        return tickers, broker

    # Scalable: p.id = ISIN, p.symbol = company name
    isin_map   = _isin_to_ticker_map(ticker_isin, broker_id)
    section    = ticker_isin.setdefault(broker_id, {})
    to_resolve = []

    for p in positions:
        isin = (getattr(p, "id", None) or "").upper()
        name = (p.symbol or "").strip()
        if not isin:
            logger.warning("  %-30s — no ISIN, skipping", name)
            continue
        if isin in isin_map:
            ticker = isin_map[isin]
            logger.info("  %-30s %s → %s (cached)", name, isin, ticker)
            tickers.append(ticker)
        else:
            to_resolve.append((name, isin))

    if to_resolve:
        logger.info("Resolving %d ISIN(s) via FMP...", len(to_resolve))
        results = await asyncio.gather(*[
            _fmp_lookup_async(isin, fmp_key) for _, isin in to_resolve
        ])
        for (name, isin), ticker in zip(to_resolve, results):
            if ticker:
                logger.info("  %-30s %s → %s (FMP)", name, isin, ticker)
                section[ticker] = isin
                isin_map[isin]  = ticker
                tickers.append(ticker)
            else:
                logger.warning("  %-30s %s → ??? (FMP returned nothing)", name, isin)

    return tickers, broker


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    from adapters.brokers.etoro_broker    import eToroBroker
    from adapters.brokers.ibkr_broker     import IBKRBroker
    from adapters.brokers.scalable_broker import ScalableBroker

    # broker_id → (cfg_name, Class, needs_isin_resolution, config_patch)
    ALL_BROKERS: dict[str, tuple] = {
        "scalable":   ("scalable", ScalableBroker, True,  {}),
        "ibkr":       ("ibkr",     IBKRBroker,     False, {"port": 4001, "is_demo": False, "client_id_broker": 50}),
        "ibkr_live":  ("ibkr",     IBKRBroker,     False, {"port": 4001, "is_demo": False, "client_id_broker": 50}),
        "ibkr_demo":  ("ibkr",     IBKRBroker,     False, {"port": 4002, "is_demo": True,  "client_id_broker": 51}),
        "etoro":      ("etoro",    eToroBroker,    False, {"is_demo": False}),
        "etoro_live": ("etoro",    eToroBroker,    False, {"is_demo": False}),
        "etoro_demo": ("etoro",    eToroBroker,    False, {"is_demo": True}),
        # "capital": ("capital", CapitalBroker, False, {}),
    }

    # Optional CLI arg: sync only one broker
    target = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if target and target not in ALL_BROKERS:
        print(f"Unknown broker '{target}'. Valid: {', '.join(ALL_BROKERS)}")
        sys.exit(1)

    brokers = {target: ALL_BROKERS[target]}.items() if target else ALL_BROKERS.items()

    fmp_key = os.environ.get("FMP_API_KEY", "").strip()
    if not fmp_key:
        logger.error("FMP_API_KEY not set")
        sys.exit(1)

    ticker_isin = _load_ticker_isin()
    watched     = _load_watched()
    portfolio   = watched.setdefault("portfolio", {})
    changed     = False

    for broker_id, (cfg_name, BrokerCls, resolve_isin, cfg_patch) in brokers:
        portfolio_key = cfg_name   # ibkr_live/ibkr_demo both store under "ibkr"
        try:
            tickers, broker_inst = await sync_broker(
                broker_id, cfg_name, BrokerCls, fmp_key, ticker_isin, resolve_isin, cfg_patch
            )
            portfolio[portfolio_key] = tickers
            changed = True
        except Exception as e:
            logger.warning("[%s] sync failed: %s", broker_id, e)
            broker_inst = None
        finally:
            if broker_inst is not None:
                try:
                    await broker_inst.disconnect()
                except Exception:
                    pass

    if changed:
        _save_ticker_isin(ticker_isin)
        _save_watched(watched)
        print("\nPortfolio:")
        print(json.dumps(portfolio, indent=2))
    else:
        logger.warning("Nothing synced — files unchanged")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
