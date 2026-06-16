#!/usr/bin/env python3
"""
NK-Parabolic Scanner
Replicates TradingView "NK-parabolic" screener:
  - US stocks
  - Price > $2
  - 1-month change > 30%
  - Market cap > $300M
  - Avg vol 30D > 500K
  - RSI(14) > 60
  - Price > SMA(10)
  - Rel vol > 1.5

Run:  python run_parabolic_scanner.py
      python run_parabolic_scanner.py --min-chg1m 40 --min-relvol 2 --limit 20
"""

import argparse
import sys
from datetime import datetime

try:
    from tradingview_screener import Query, col
    import pandas as pd
except ImportError:
    print("Missing deps. Run:  pip install tradingview-screener pandas")
    sys.exit(1)

DEFAULT_LIMIT       = 50
DEFAULT_MIN_CHG1M   = 30.0
DEFAULT_MIN_PRICE   = 2.0
DEFAULT_MIN_MKTCAP  = 300_000_000
DEFAULT_MIN_AVGVOL  = 500_000
DEFAULT_MIN_RSI     = 60.0
DEFAULT_MIN_RELVOL  = 1.5


def fmt_mktcap(v):
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    return f"${v/1e6:.0f}M"

def fmt_vol(v):
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(int(v))


def run_scanner(
    limit=DEFAULT_LIMIT,
    min_chg1m=DEFAULT_MIN_CHG1M,
    min_price=DEFAULT_MIN_PRICE,
    min_mktcap=DEFAULT_MIN_MKTCAP,
    min_avgvol=DEFAULT_MIN_AVGVOL,
    min_rsi=DEFAULT_MIN_RSI,
    min_relvol=DEFAULT_MIN_RELVOL,
):
    print(f"\n{'─'*70}")
    print(f"  NK-PARABOLIC SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'─'*70}")
    print(f"  Filters: Price>${min_price} | MCap>${fmt_mktcap(min_mktcap)} | "
          f"AvgVol>{fmt_vol(min_avgvol)}")
    print(f"           Chg1M>{min_chg1m}% | RSI>{min_rsi} | Price>SMA10 | RelVol>{min_relvol}")
    print(f"{'─'*70}\n")

    COLS = [
        "name", "description", "close", "change", "Perf.1M",
        "volume", "relative_volume_10d_calc",
        "market_cap_basic", "average_volume_30d_calc",
        "RSI", "SMA10",
        "price_earnings_ttm", "earnings_per_share_diluted_ttm",
        "earnings_per_share_diluted_yoy_growth_ttm",
        "sector", "analyst_rating_us",
    ]

    _, df = (
        Query()
        .set_markets("america")
        .select(*COLS)
        .where(
            col("close")                        > min_price,
            col("market_cap_basic")             > min_mktcap,
            col("average_volume_30d_calc")      > min_avgvol,
            col("Perf.1M")                      > min_chg1m,
            col("RSI")                          > min_rsi,
            col("relative_volume_10d_calc")     > min_relvol,
            col("close")                        > col("SMA10"),
        )
        .order_by("Perf.1M", ascending=False)
        .limit(limit)
        .get_scanner_data()
    )

    if df.empty:
        print("  No results found.\n")
        return df

    print(f"  {'#':<3} {'TICKER':<8} {'NAME':<26} {'PRICE':>7} {'TODAY':>7} "
          f"{'1M':>7} {'VOL':>8} {'RVOL':>5} {'MKTCAP':>9} "
          f"{'RSI':>5} {'P/E':>7}  SECTOR              RATING")
    print(f"  {'─'*130}")

    for i, (_, row) in enumerate(df.iterrows(), 1):
        chg      = row.get("change", 0) or 0
        chg1m    = row.get("Perf.1M", 0) or 0
        rsi      = row.get("RSI", 0) or 0
        pe       = row.get("price_earnings_ttm", None)
        relvol   = row.get("relative_volume_10d_calc", 0) or 0
        name     = str(row.get("description", ""))[:24]
        sector   = str(row.get("sector", ""))[:18]
        rating   = str(row.get("analyst_rating_us", ""))
        pe_str   = f"{pe:.1f}" if pe and pe > 0 else "—"

        print(
            f"  {i:<3} {row['name']:<8} {name:<26} "
            f"${row['close']:>6.2f} "
            f"{chg:>+6.2f}% "
            f"{chg1m:>+6.1f}% "
            f"{fmt_vol(row['volume']):>8} "
            f"{relvol:>4.1f}x "
            f"{fmt_mktcap(row['market_cap_basic']):>9} "
            f"{rsi:>5.1f} "
            f"{pe_str:>7}  "
            f"{sector:<20} {rating}"
        )

    print(f"\n  {'─'*70}")
    print(f"  Total: {len(df)} parabolic movers found")
    print(f"  {'─'*70}\n")

    return df


def main():
    parser = argparse.ArgumentParser(description="NK-Parabolic Scanner")
    parser.add_argument("--limit",       type=int,   default=DEFAULT_LIMIT,      help="Max results (default 50)")
    parser.add_argument("--min-chg1m",   type=float, default=DEFAULT_MIN_CHG1M,  help="Min 1-month change %% (default 30)")
    parser.add_argument("--min-price",   type=float, default=DEFAULT_MIN_PRICE,  help="Min price (default 2)")
    parser.add_argument("--min-mktcap",  type=float, default=DEFAULT_MIN_MKTCAP, help="Min mkt cap (default 300M)")
    parser.add_argument("--min-avgvol",  type=float, default=DEFAULT_MIN_AVGVOL, help="Min avg vol 30D (default 500K)")
    parser.add_argument("--min-rsi",     type=float, default=DEFAULT_MIN_RSI,    help="Min RSI(14) (default 60)")
    parser.add_argument("--min-relvol",  type=float, default=DEFAULT_MIN_RELVOL, help="Min rel vol (default 1.5)")
    parser.add_argument("--csv",         action="store_true",                    help="Save results to CSV")
    args = parser.parse_args()

    df = run_scanner(
        limit=args.limit,
        min_chg1m=args.min_chg1m,
        min_price=args.min_price,
        min_mktcap=args.min_mktcap,
        min_avgvol=args.min_avgvol,
        min_rsi=args.min_rsi,
        min_relvol=args.min_relvol,
    )

    if args.csv and not df.empty:
        fname = f"parabolic_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(fname, index=False)
        print(f"  Saved → {fname}\n")


if __name__ == "__main__":
    main()
