"""
services.morning_enrichment — MorningEnrichmentService

Reads the daily post-market movers CSV, enriches it with 5-day OHLC from
yfinance (TradingView only provides 3 days), and generates filled prompt
text files ready to paste into an AI tool with web access.

Produced files:
    data/daily_morning/prompts/DD.MM.YYYY_prompt1.txt
    data/daily_morning/prompts/DD.MM.YYYY_prompt2.txt
    data/daily_morning/prompts/DD.MM.YYYY_prompt3.txt
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

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

    def __init__(self, data_root: Path) -> None:
        self._data_root   = data_root
        self._movers_dir  = data_root / "daily_morning" / "post-market-movers"
        self._prompts_dir = data_root / "daily_morning" / "prompts"

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
                f"No movers CSV found for {run_date.strftime('%d.%m.%Y')} "
                f"in {self._movers_dir}"
            )

        logger.info("[Enrichment] reading %s", csv_path.name)
        df = pd.read_csv(csv_path, sep=_detect_sep(csv_path))
        df = _normalise_columns(df)

        # Build 5-day OHLC map symbol → list[dict] (oldest bar first)
        symbols  = df["name"].dropna().tolist()
        ohlc_map = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_5day_ohlc, symbols, df
        )

        ticker_list = ", ".join(symbols)
        ticker_data = self._format_all_tickers(df, ohlc_map)

        from prompts.post_market_morning import build_prompt
        prompt_text = build_prompt(
            prompt_num   = prompt_num,
            ticker_data  = ticker_data,
            ticker_list  = ticker_list,
        )

        self._prompts_dir.mkdir(parents=True, exist_ok=True)
        date_str  = run_date.strftime("%d.%m.%Y")
        out_path  = self._prompts_dir / f"{date_str}_prompt{prompt_num}.txt"
        out_path.write_text(prompt_text, encoding="utf-8")
        logger.info("[Enrichment] saved prompt %d → %s", prompt_num, out_path)
        return out_path

    # ── CSV discovery ─────────────────────────────────────────────────────────

    def _find_movers_csv(self, run_date: dt.date) -> Optional[Path]:
        """
        Look for DD.MM.YYYY_post_movers.csv for run_date.
        Falls back to the most recent file in the directory if today's is missing
        (handles the case where the scheduler fires for prompts but the scan CSV
        was written under a slightly different date).
        """
        preferred = self._movers_dir / f"{run_date.strftime('%d.%m.%Y')}_post_movers.csv"
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

        for symbol in symbols:
            try:
                hist = yf.Ticker(symbol).history(period="10d", interval="1d", auto_adjust=True)
                if hist.empty:
                    result[symbol] = []
                    continue

                hist = hist.tail(5)  # last 5 trading sessions
                bars: list[dict] = []
                for i, (date_idx, row) in enumerate(hist.iterrows()):
                    is_today = (i == len(hist) - 1)
                    bar_date = date_idx.date() if hasattr(date_idx, "date") else date_idx

                    # For the most recent bar, prefer TV close if it's more current
                    # (yfinance "today" bar may be yesterday's if fetched pre-open)
                    close = float(row["Close"])
                    if is_today and not tv_idx.empty and symbol in tv_idx.index:
                        tv_row  = tv_idx.loc[symbol]
                        tv_close = tv_row.get("close")
                        if pd.notna(tv_close):
                            close = float(tv_close)

                    bars.append({
                        "date":   bar_date.strftime("%b %d %a"),
                        "open":   float(row["Open"]),
                        "high":   float(row["High"]),
                        "low":    float(row["Low"]),
                        "close":  close,
                        "volume": float(row["Volume"]),
                        "today":  is_today,
                    })

                result[symbol] = bars

            except Exception as exc:
                logger.warning("[Enrichment] %s yfinance error: %s", symbol, exc)
                result[symbol] = []

        return result

    # ── Formatting ────────────────────────────────────────────────────────────

    def _format_all_tickers(
        self,
        df: pd.DataFrame,
        ohlc_map: dict[str, list[dict]],
    ) -> str:
        from prompts.post_market_morning import format_ticker_block
        blocks = []
        for _, row in df.iterrows():
            sym  = row.get("name", "")
            ohlc = ohlc_map.get(sym)
            blocks.append(format_ticker_block(row, ohlc))
        return "\n\n".join(blocks)
