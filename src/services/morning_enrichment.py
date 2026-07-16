"""
services.morning_enrichment — MorningEnrichmentService

Reads the daily post-market movers CSV, enriches it with 5-day OHLC from
yfinance (TradingView only provides 3 days), and generates filled prompt
text files ready to paste into an AI tool with web access.

Prompt 3 additionally embeds today's live 5-min session data (bars, VWAP,
opening range, gap status) fetched via PriceHistoryService / FMP.

Produced files:
    data/daily_morning/prompts/MM.DD.YYYY_prompt1.txt
    data/daily_morning/prompts/MM.DD.YYYY_prompt2.txt
    data/daily_morning/prompts/MM.DD.YYYY_prompt3.txt
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd

if TYPE_CHECKING:
    from services.price_history_service import PriceHistoryService

logger = logging.getLogger(__name__)

# Maps TradingView manual-export column names → scanner API column names.
# Only columns that exist in the file are renamed; missing ones are left absent
# (format_ticker_block already renders them as "—").
_TV_EXPORT_MAP = {
    "Symbol":                  "name",
    "Description":             "description",
    "Post-market change %":    "postmarket_change",
    "Post-market volume":      "postmarket_volume",
    "Post-market price":       "postmarket_close",
    "Price":                   "close",
    "Price change %, 1 day":   "change",
    "Volume, 1 day":           "volume",
    "Sector":                  "sector",
}


def _detect_sep(path: Path) -> str:
    """Return '\t' if the file's first line has more tabs than commas, else ','."""
    first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    return "\t" if first.count("\t") > first.count(",") else ","


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename TradingView manual-export headers to scanner API names if present."""
    rename = {k: v for k, v in _TV_EXPORT_MAP.items() if k in df.columns}
    return df.rename(columns=rename)


class MorningEnrichmentService:

    def __init__(
        self,
        data_root: Path,
        price_history_svc: Optional["PriceHistoryService"] = None,
        news_svc=None,
    ) -> None:
        self._data_root        = data_root
        self._movers_dir       = data_root / "daily_morning" / "post-market-movers"
        self._prompts_dir      = data_root / "daily_morning" / "prompts"
        self._price_history_svc = price_history_svc
        self._news_svc         = news_svc

    # ── Public API ────────────────────────────────────────────────────────────

    async def build_and_save_prompt(
        self,
        prompt_num: int,
        run_date: dt.date,
    ) -> Path:
        """
        Main entry point called by the scheduler.

        Finds today's movers CSV, fetches 5-day OHLC from yfinance, fills the
        prompt template, saves the file, and returns the path.

        Raises FileNotFoundError if today's movers CSV doesn't exist yet.
        Raises ValueError for invalid prompt_num (must be 1–3).
        """
        if prompt_num not in (1, 2, 3):
            raise ValueError(f"prompt_num must be 1, 2, or 3 — got {prompt_num!r}")

        csv_path = self._find_movers_csv(run_date)
        if csv_path is None:
            raise FileNotFoundError(
                f"No movers CSV found for {run_date.strftime('%m.%d.%Y')} "
                f"in {self._movers_dir}"
            )

        logger.info("[Enrichment] reading %s", csv_path.name)
        df = pd.read_csv(csv_path, sep=_detect_sep(csv_path))
        df = _normalise_columns(df)
        df = self._filter_top_movers(df, n=5)

        # Build 5-day OHLC map symbol → list[dict] (oldest bar first)
        symbols  = df["name"].dropna().tolist()
        ohlc_map = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_5day_ohlc, symbols, df
        )

        intraday_map: dict[str, dict] = {}
        if prompt_num == 3:
            intraday_map = await self._fetch_intraday_5m_async(symbols, df)

        news_map: dict[str, list[dict]] = {}
        if self._news_svc is not None:
            news_map = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_news, symbols
            )

        ticker_list = ", ".join(symbols)
        ticker_data = self._format_all_tickers(df, ohlc_map, intraday_map or None, news_map or None)

        from prompts.post_market_morning import build_prompt
        prompt_text = build_prompt(
            prompt_num   = prompt_num,
            ticker_data  = ticker_data,
            ticker_list  = ticker_list,
        )

        self._prompts_dir.mkdir(parents=True, exist_ok=True)
        date_str  = run_date.strftime("%m.%d.%Y")
        out_path  = self._prompts_dir / f"{date_str}_prompt{prompt_num}.txt"
        out_path.write_text(prompt_text, encoding="utf-8")
        logger.info("[Enrichment] saved prompt %d → %s", prompt_num, out_path)
        return out_path

    # ── Top-mover filter ─────────────────────────────────────────────────────

    def _filter_top_movers(self, df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
        """
        Score = postmarket_volume × |postmarket_change|.
        Returns top-n risers (PM change > 0) and top-n fallers (PM change < 0),
        each group sorted by score descending. Order: risers first, then fallers.
        Falls back to returning the full df if the required columns are absent.
        """
        if "postmarket_change" not in df.columns or "postmarket_volume" not in df.columns:
            logger.warning("[Enrichment] PM change/volume columns missing — skipping top-mover filter")
            return df

        pm_chg = pd.to_numeric(df["postmarket_change"], errors="coerce")
        pm_vol = pd.to_numeric(df["postmarket_volume"], errors="coerce").fillna(0)
        score  = pm_vol * pm_chg.abs()

        risers  = df[pm_chg > 0].assign(_score=score).nlargest(n, "_score").drop(columns="_score")
        fallers = df[pm_chg < 0].assign(_score=score).nlargest(n, "_score").drop(columns="_score")
        result  = pd.concat([risers, fallers], ignore_index=True)
        logger.info(
            "[Enrichment] top-mover filter: %d risers + %d fallers selected (from %d total)",
            len(risers), len(fallers), len(df),
        )
        return result

    # ── CSV discovery ─────────────────────────────────────────────────────────

    def _find_movers_csv(self, run_date: dt.date) -> Optional[Path]:
        """
        Look for MM.DD.YYYY_post_movers.csv for run_date.
        Falls back to the most recent file in the directory if today's is missing
        (handles the case where the scheduler fires for prompts but the scan CSV
        was written under a slightly different date).
        """
        preferred = self._movers_dir / f"{run_date.strftime('%m.%d.%Y')}_post_movers.csv"
        if preferred.exists():
            return preferred

        # Fallback: find the most recently modified CSV in the movers directory
        candidates = sorted(
            self._movers_dir.glob("*_post_movers.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            logger.warning(
                "[Enrichment] %s not found — falling back to %s",
                preferred.name, candidates[0].name,
            )
            return candidates[0]

        return None

    # ── yfinance OHLC fetch ───────────────────────────────────────────────────

    def _fetch_5day_ohlc(
        self,
        symbols: list[str],
        tv_df: pd.DataFrame,
    ) -> dict[str, list[dict]]:
        """
        Synchronous (runs in executor).

        Fetches up to 10 calendar days of daily bars per symbol via yfinance,
        returns the last 5 trading sessions as a list of bar dicts (oldest first).

        Days 0–2 are already in the TV CSV as close/close[1]/close[2]; we prefer
        yfinance for all 5 to get clean date labels and consistent data.
        TV OHLC values are used as a fallback for day 0 if yfinance is stale.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("[Enrichment] yfinance not installed — skipping 5-day OHLC")
            return {}

        result: dict[str, list[dict]] = {}

        # Index tv_df by symbol for fast per-row access
        tv_idx = tv_df.set_index("name") if "name" in tv_df.columns else pd.DataFrame()

        today      = dt.date.today()
        # end is exclusive in yfinance — so bars only go up to yesterday,
        # ensuring every returned session is a fully-settled historical bar
        # (no partial open/high/low from an in-progress or just-closed session).
        end_date   = today
        start_date = today - dt.timedelta(days=14)  # enough to cover 5 trading days

        for symbol in symbols:
            try:
                hist = yf.Ticker(symbol).history(
                    start=start_date.isoformat(),
                    end=end_date.isoformat(),
                    interval="1d",
                    auto_adjust=False,
                )
                if hist.empty:
                    result[symbol] = []
                    continue

                # Leave room for the "today" placeholder appended below so the
                # returned list is 5 bars total, not 6.
                hist = hist.tail(4)
                bars: list[dict] = []
                for date_idx, row in hist.iterrows():
                    bar_date = date_idx.date() if hasattr(date_idx, "date") else date_idx
                    bars.append({
                        "date":   bar_date.strftime("%b %d %a"),
                        "open":   float(row["Open"]),
                        "high":   float(row["High"]),
                        "low":    float(row["Low"]),
                        "close":  float(row["Close"]),
                        "volume": float(row["Volume"]),
                        "today":  False,
                    })

                # Always append today as a placeholder — market may be open (partial)
                # or not yet open; either way the historical bars above are complete.
                # yfinance has no bar for today yet, so use the TV CSV's live close.
                tv_close = None
                if not tv_idx.empty and symbol in tv_idx.index:
                    raw_close = tv_idx.loc[symbol].get("close") if hasattr(tv_idx.loc[symbol], "get") else None
                    try:
                        tv_close = float(raw_close) if raw_close is not None else None
                    except (TypeError, ValueError):
                        tv_close = None

                bars.append({
                    "date":   today.strftime("%b %d %a"),
                    "open":   None,
                    "high":   None,
                    "low":    None,
                    "close":  tv_close,
                    "volume": None,
                    "today":  True,
                })

                result[symbol] = bars

            except Exception as exc:
                logger.warning("[Enrichment] %s yfinance error: %s", symbol, exc)
                result[symbol] = []

        return result

    # ── Intraday 5-min fetch (FMP via PriceHistoryService) ───────────────────

    async def _fetch_intraday_5m_async(
        self,
        symbols: list[str],
        tv_df: pd.DataFrame,
    ) -> dict[str, dict]:
        """
        Fetch today's 5-min bars for every symbol in parallel via PriceHistoryService
        (FMP). Computes cumulative VWAP, opening-range high/low (9:30–10:00 ET),
        current price, gap status, and 30-min volume.

        Returns {} if price_history_svc was not injected.
        FMP intraday timestamps are naive datetimes in US/Eastern time.
        """
        if self._price_history_svc is None:
            logger.warning("[Enrichment] PriceHistoryService not set — skipping intraday")
            return {}

        from core.entities.time_frame import TimeFrame

        tv_idx = tv_df.set_index("name") if "name" in tv_df.columns else pd.DataFrame()
        _empty: dict = {
            "bars": [], "vwap": None, "or_high": None, "or_low": None,
            "current_price": None, "above_vwap": None, "gap_status": None, "vol_30m": None,
        }

        _SESSION_START = dt.time(9, 30)
        _SESSION_END   = dt.time(16, 0)

        def _lookup_closes(symbol: str) -> tuple[float | None, float | None]:
            if tv_idx.empty or symbol not in tv_idx.index:
                return None, None
            r = tv_idx.loc[symbol]
            raw_pc = r.get("close")            if hasattr(r, "get") else (r["close"]            if "close"            in r.index else None)
            raw_pm = r.get("postmarket_close") if hasattr(r, "get") else (r["postmarket_close"] if "postmarket_close" in r.index else None)
            prior_close = pm_close = None
            try:    prior_close = float(raw_pc) if raw_pc is not None else None
            except (TypeError, ValueError): pass
            try:    pm_close    = float(raw_pm) if raw_pm is not None else None
            except (TypeError, ValueError): pass
            return prior_close, pm_close

        async def _fetch_one(symbol: str) -> tuple[str, dict]:
            try:
                raw_bars = await self._price_history_svc.get_intraday_bars(
                    symbol, TimeFrame.MINUTE_5
                )
                # Filter to regular session (FMP timestamps are naive ET)
                reg_bars = [
                    b for b in raw_bars
                    if b.time and _SESSION_START <= b.time.time() < _SESSION_END
                ]
                if not reg_bars:
                    return symbol, dict(_empty)

                # Cumulative session VWAP
                cum_tpv = 0.0
                cum_vol = 0.0
                bars: list[dict] = []
                for b in reg_bars:
                    vol      = b.volume or 0.0
                    cum_tpv += ((b.high + b.low + b.close) / 3.0) * vol
                    cum_vol += vol
                    bars.append({
                        "time":   b.time.strftime("%H:%M") if b.time else "—",
                        "open":   b.open,
                        "high":   b.high,
                        "low":    b.low,
                        "close":  b.close,
                        "volume": vol,
                        "vwap":   (cum_tpv / cum_vol) if cum_vol > 0 else None,
                    })

                # Opening range = bars that open before 10:00 (i.e., 9:30–9:55 candles)
                or_bars = [b for b in bars if b["time"] < "10:00"]
                or_high = max(b["high"]   for b in or_bars) if or_bars else None
                or_low  = min(b["low"]    for b in or_bars) if or_bars else None
                vol_30m = sum(b["volume"] for b in or_bars) if or_bars else None

                current_price = bars[-1]["close"] if bars else None
                current_vwap  = bars[-1]["vwap"]  if bars else None
                above_vwap    = (
                    (current_price > current_vwap)
                    if (current_price is not None and current_vwap is not None)
                    else None
                )

                prior_close, pm_close = _lookup_closes(symbol)
                gap_status: str | None = None
                if current_price is not None and prior_close is not None:
                    if current_price <= prior_close:
                        gap_status = "full_fill"
                    elif pm_close is not None and current_price >= pm_close:
                        gap_status = "holding"
                    else:
                        gap_status = "partial_fill"

                return symbol, {
                    "bars":          bars,
                    "vwap":          current_vwap,
                    "or_high":       or_high,
                    "or_low":        or_low,
                    "current_price": current_price,
                    "above_vwap":    above_vwap,
                    "gap_status":    gap_status,
                    "vol_30m":       vol_30m,
                }

            except Exception as exc:
                logger.warning("[Enrichment] %s intraday error: %s", symbol, exc)
                return symbol, dict(_empty)

        pairs = await asyncio.gather(*(_fetch_one(sym) for sym in symbols))
        return dict(pairs)

    # ── Formatting ────────────────────────────────────────────────────────────

    def _fetch_news(self, symbols: list[str]) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for sym in symbols:
            try:
                items = self._news_svc.get_news(sym, lookback_days=2)
                result[sym] = [
                    {
                        "ts":     n.published_date.strftime("%m/%d %H:%M"),
                        "source": n.publisher or n.site or "",
                        "title":  n.title,
                    }
                    for n in items[:5]
                ]
            except Exception as e:
                logger.warning("[Enrichment] news fetch failed for %s: %s", sym, e)
        return result

    def _format_all_tickers(
        self,
        df: pd.DataFrame,
        ohlc_map: dict[str, list[dict]],
        intraday_map: dict[str, dict] | None = None,
        news_map: dict[str, list[dict]] | None = None,
    ) -> str:
        from prompts.post_market_morning import format_ticker_block
        blocks = []
        for _, row in df.iterrows():
            sym      = row.get("name", "")
            ohlc     = ohlc_map.get(sym)
            intraday = intraday_map.get(sym) if intraday_map else None
            news     = news_map.get(sym) if news_map else None
            blocks.append(format_ticker_block(row, ohlc, intraday, news))
        return "\n\n".join(blocks)
