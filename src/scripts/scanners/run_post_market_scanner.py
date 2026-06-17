#!/usr/bin/env python3
"""
Post-Market Movers Scanner
Replicates TradingView "nk-post-market-movers" screener:
  - US stocks
  - Price > $2
  - Market cap > $300M
  - Avg vol 30D > 500K
  - Post-mkt change OUTSIDE -3% to +3%  (|chg| > 3%)
  - Post-mkt volume > 100K

Output is always saved to:
  <project_root>/data/daily_morning/post-market-movers/DD.MM.YYYY_post_movers.csv

Install: pip install tradingview-screener pandas tabulate
Run:     python run_post_market_scanner.py
         python run_post_market_scanner.py --limit 30 --min-pmchg 5
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

try:
    from tradingview_screener import Query, col
    import pandas as pd
except ImportError:
    print("Missing deps. Run:  pip install tradingview-screener pandas tabulate")
    sys.exit(1)

# ── Paths ────────────────────────────────────────────────────────────────────
_ROOT       = Path(__file__).resolve().parents[3]
_MOVERS_DIR = _ROOT / "data" / "daily_morning" / "post-market-movers"

# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_LIMIT       = 50
DEFAULT_MIN_PMCHG   = 3.0
DEFAULT_MIN_PRICE   = 2.0
DEFAULT_MIN_MKTCAP  = 300_000_000
DEFAULT_MIN_AVGVOL  = 500_000
DEFAULT_MIN_PMVOL   = 100_000
# ────────────────────────────────────────────────────────────────────────────

# All columns fetched from TradingView screener
_COLS = [
    # identity
    "name", "description", "sector", "type", "exchange",
    # current session
    "open", "high", "low", "close", "change", "volume",
    # post-market
    "postmarket_change", "postmarket_volume", "postmarket_close",
    # prior sessions (TradingView provides up to 2 bars back)
    "open[1]", "high[1]", "low[1]", "close[1]", "volume[1]",
    "open[2]", "high[2]", "low[2]", "close[2]", "volume[2]",
    # technicals
    "RSI", "ADX", "ADX+DI", "ADX-DI", "EMA20", "EMA50",
    # volume context
    "average_volume_10d_calc", "average_volume_30d_calc",
    "relative_volume_intraday|5",
    # fundamentals
    "market_cap_basic",
]


def fmt_mktcap(v):
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    return f"${v/1e6:.0f}M"

def fmt_vol(v):
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(int(v))

def arrow(v):
    return "▲" if v > 0 else "▼"


def run_scanner(
    limit=DEFAULT_LIMIT,
    min_pmchg=DEFAULT_MIN_PMCHG,
    min_price=DEFAULT_MIN_PRICE,
    min_mktcap=DEFAULT_MIN_MKTCAP,
    min_avgvol=DEFAULT_MIN_AVGVOL,
    min_pmvol=DEFAULT_MIN_PMVOL,
) -> pd.DataFrame:
    print(f"\n{'─'*60}")
    print(f"  POST-MARKET MOVERS SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'─'*60}")
    print(f"  Filters: Price>${min_price} | MCap>${fmt_mktcap(min_mktcap)} | "
          f"AvgVol>{fmt_vol(min_avgvol)} | |PM Chg|>{min_pmchg}% | PM Vol>{fmt_vol(min_pmvol)}")
    print(f"{'─'*60}\n")

    base_filters = [
        col("close") > min_price,
        col("market_cap_basic") > min_mktcap,
        col("average_volume_30d_calc") > min_avgvol,
        col("postmarket_volume") > min_pmvol,
    ]

    def _query(extra_filter):
        _, frame = (
            Query()
            .set_markets("america")
            .select(*_COLS)
            .where(*base_filters, extra_filter)
            .order_by("postmarket_volume", ascending=False)
            .limit(500)
            .get_scanner_data()
        )
        return frame

    df = pd.concat([
        _query(col("postmarket_change") >  min_pmchg),
        _query(col("postmarket_change") < -min_pmchg),
    ], ignore_index=True).drop_duplicates(subset="name")

    df = df.sort_values("postmarket_change", ascending=False).head(limit)

    if df.empty:
        print("  No results found. Market may be closed / pre-market not active.\n")
        return df

    gainers = df[df["postmarket_change"] > 0].copy()
    losers  = df[df["postmarket_change"] < 0].copy()

    def print_section(subset, label):
        if subset.empty:
            return
        print(f"\n  {label} ({len(subset)})")
        print(f"  {'─'*110}")
        print(f"  {'#':<3} {'TICKER':<8} {'NAME':<28} {'PRICE':>7} {'REG CHG':>8} {'PM CHG':>8} "
              f"{'PM VOL':>9} {'AVG VOL':>9} {'MKTCAP':>10}  SECTOR")
        print(f"  {'─'*110}")
        for i, (_, row) in enumerate(subset.iterrows(), 1):
            pmchg = row["postmarket_change"]
            name  = str(row.get("description", ""))[:26]
            print(
                f"  {i:<3} {row['name']:<8} {name:<28} "
                f"${row['close']:>6.2f} "
                f"{row['change']:>+7.2f}% "
                f"{arrow(pmchg)}{abs(pmchg):>6.2f}% "
                f"{fmt_vol(row['postmarket_volume']):>9} "
                f"{fmt_vol(row['average_volume_30d_calc']):>9} "
                f"{fmt_mktcap(row['market_cap_basic']):>10}  "
                f"{row.get('sector','')}"
            )

    print_section(gainers, "🟢 GAINERS")
    print_section(losers,  "🔴 LOSERS")

    print(f"\n  {'─'*60}")
    print(f"  Total: {len(df)} results  |  Gainers: {len(gainers)}  |  Losers: {len(losers)}")
    print(f"  {'─'*60}\n")

    return df


def save_results(df: pd.DataFrame, date: datetime | None = None) -> Path:
    """Save scanner results to the standard daily movers directory."""
    _MOVERS_DIR.mkdir(parents=True, exist_ok=True)
    ts = (date or datetime.now()).strftime("%d.%m.%Y")
    path = _MOVERS_DIR / f"{ts}_post_movers.csv"
    df.to_csv(path, index=False)
    print(f"  Saved → {path}\n")
    return path


def run_and_save(
    limit=DEFAULT_LIMIT,
    min_pmchg=DEFAULT_MIN_PMCHG,
    min_price=DEFAULT_MIN_PRICE,
    min_mktcap=DEFAULT_MIN_MKTCAP,
    min_avgvol=DEFAULT_MIN_AVGVOL,
    min_pmvol=DEFAULT_MIN_PMVOL,
) -> tuple[pd.DataFrame, Path | None]:
    """Run scanner and always save results. Returns (df, saved_path)."""
    df = run_scanner(
        limit=limit,
        min_pmchg=min_pmchg,
        min_price=min_price,
        min_mktcap=min_mktcap,
        min_avgvol=min_avgvol,
        min_pmvol=min_pmvol,
    )
    if df.empty:
        return df, None
    path = save_results(df)
    return df, path


def main():
    parser = argparse.ArgumentParser(description="Post-Market Movers Scanner")
    parser.add_argument("--limit",      type=int,   default=DEFAULT_LIMIT,      help="Max results (default 50)")
    parser.add_argument("--min-pmchg",  type=float, default=DEFAULT_MIN_PMCHG,  help="Min |PM change| %% (default 3)")
    parser.add_argument("--min-price",  type=float, default=DEFAULT_MIN_PRICE,  help="Min price (default 2)")
    parser.add_argument("--min-mktcap", type=float, default=DEFAULT_MIN_MKTCAP, help="Min mkt cap (default 300M)")
    parser.add_argument("--min-avgvol", type=float, default=DEFAULT_MIN_AVGVOL, help="Min avg vol 30D (default 500K)")
    parser.add_argument("--min-pmvol",  type=float, default=DEFAULT_MIN_PMVOL,  help="Min PM volume (default 100K)")
    args = parser.parse_args()

    _, saved = run_and_save(
        limit=args.limit,
        min_pmchg=args.min_pmchg,
        min_price=args.min_price,
        min_mktcap=args.min_mktcap,
        min_avgvol=args.min_avgvol,
        min_pmvol=args.min_pmvol,
    )
    if saved:
        print(f"  Results saved to: {saved}\n")


if __name__ == "__main__":
    main()
