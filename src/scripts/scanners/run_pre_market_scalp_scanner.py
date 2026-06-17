#!/usr/bin/env python3
"""
Pre-Market Scalp Scanner
Replicates TradingView "nk-pre-market-scalph" screener:
  - US stocks
  - Price $2–$30
  - Market cap > $300M
  - Avg vol 30D > 500K
  - Pre-mkt change > +10%
  - Pre-mkt volume > 100K
  - Float 10M–100M shares

Run:  python run_pre_market_scalp_scanner.py
      python run_pre_market_scalp_scanner.py --limit 30 --min-pmchg 15
"""

import argparse
import sys
from datetime import datetime

try:
    from tradingview_screener import Query, col
except ImportError:
    print("Missing deps. Run:  pip install tradingview-screener pandas tabulate")
    sys.exit(1)

DEFAULT_LIMIT      = 50
DEFAULT_MIN_PMCHG  = 10.0
DEFAULT_MIN_PRICE  = 2.0
DEFAULT_MAX_PRICE  = 30.0
DEFAULT_MIN_MKTCAP = 300_000_000
DEFAULT_MIN_AVGVOL = 500_000
DEFAULT_MIN_PMVOL  = 100_000
DEFAULT_MIN_FLOAT  = 10_000_000
DEFAULT_MAX_FLOAT  = 100_000_000

_COLS = [
    "name", "description", "close", "change", "volume",
    "market_cap_basic", "average_volume_30d_calc",
    "premarket_change", "premarket_volume",
    "float_shares_outstanding", "sector", "exchange",
]


def fmt_mktcap(v):
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    return f"${v/1e6:.0f}M"

def fmt_vol(v):
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(int(v))

def fmt_float(v):
    if v is None: return "N/A"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(int(v))


def run_scanner(
    limit=DEFAULT_LIMIT,
    min_pmchg=DEFAULT_MIN_PMCHG,
    min_price=DEFAULT_MIN_PRICE,
    max_price=DEFAULT_MAX_PRICE,
    min_mktcap=DEFAULT_MIN_MKTCAP,
    min_avgvol=DEFAULT_MIN_AVGVOL,
    min_pmvol=DEFAULT_MIN_PMVOL,
    min_float=DEFAULT_MIN_FLOAT,
    max_float=DEFAULT_MAX_FLOAT,
):
    print(f"\n{'─'*70}")
    print(f"  PRE-MARKET SCALP SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'─'*70}")
    print(f"  Filters: Price ${min_price}–${max_price} | MCap>{fmt_mktcap(min_mktcap)} | "
          f"AvgVol>{fmt_vol(min_avgvol)}")
    print(f"           PM Chg>+{min_pmchg}% | PM Vol>{fmt_vol(min_pmvol)} | "
          f"Float {fmt_float(min_float)}–{fmt_float(max_float)}")
    print(f"{'─'*70}\n")

    filters = [
        col("close").between(min_price, max_price),
        col("market_cap_basic") > min_mktcap,
        col("average_volume_30d_calc") > min_avgvol,
        col("premarket_volume") > min_pmvol,
        col("premarket_change") > min_pmchg,
        col("float_shares_outstanding").between(min_float, max_float),
    ]

    _, df = (
        Query()
        .set_markets("america")
        .select(*_COLS)
        .where(*filters)
        .order_by("premarket_volume", ascending=False)
        .limit(limit)
        .get_scanner_data()
    )

    if df.empty:
        print("  No results found. Market may be closed / pre-market not active.\n")
        return df

    print(f"  {'#':<3} {'TICKER':<8} {'NAME':<28} {'PRICE':>7} {'REG CHG':>8} "
          f"{'PM CHG':>8} {'PM VOL':>9} {'FLOAT':>9} {'MKTCAP':>10}  SECTOR")
    print(f"  {'─'*120}")

    for i, (_, row) in enumerate(df.iterrows(), 1):
        pmchg  = row.get("premarket_change") or 0
        name   = str(row.get("description", ""))[:26]
        fshares = row.get("float_shares_outstanding")
        print(
            f"  {i:<3} {row['name']:<8} {name:<28} "
            f"${row['close']:>6.2f} "
            f"{(row.get('change') or 0):>+7.2f}% "
            f"+{pmchg:>6.2f}% "
            f"{fmt_vol(row.get('premarket_volume') or 0):>9} "
            f"{fmt_float(fshares):>9} "
            f"{fmt_mktcap(row.get('market_cap_basic') or 0):>10}  "
            f"{row.get('sector', '')}"
        )

    print(f"\n  {'─'*70}")
    print(f"  Total: {len(df)} results")
    print(f"  {'─'*70}\n")

    return df


def main():
    parser = argparse.ArgumentParser(description="Pre-Market Scalp Scanner (nk-pre-market-scalph)")
    parser.add_argument("--limit",      type=int,   default=DEFAULT_LIMIT,      help="Max results (default 50)")
    parser.add_argument("--min-pmchg",  type=float, default=DEFAULT_MIN_PMCHG,  help="Min PM change %% (default 10)")
    parser.add_argument("--min-price",  type=float, default=DEFAULT_MIN_PRICE,  help="Min price (default 2)")
    parser.add_argument("--max-price",  type=float, default=DEFAULT_MAX_PRICE,  help="Max price (default 30)")
    parser.add_argument("--min-mktcap", type=float, default=DEFAULT_MIN_MKTCAP, help="Min mkt cap (default 300M)")
    parser.add_argument("--min-avgvol", type=float, default=DEFAULT_MIN_AVGVOL, help="Min avg vol 30D (default 500K)")
    parser.add_argument("--min-pmvol",  type=float, default=DEFAULT_MIN_PMVOL,  help="Min pre-mkt volume (default 100K)")
    parser.add_argument("--min-float",  type=float, default=DEFAULT_MIN_FLOAT,  help="Min float shares (default 10M)")
    parser.add_argument("--max-float",  type=float, default=DEFAULT_MAX_FLOAT,  help="Max float shares (default 100M)")
    parser.add_argument("--csv",        action="store_true",                    help="Save results to CSV")
    args = parser.parse_args()

    df = run_scanner(
        limit=args.limit,
        min_pmchg=args.min_pmchg,
        min_price=args.min_price,
        max_price=args.max_price,
        min_mktcap=args.min_mktcap,
        min_avgvol=args.min_avgvol,
        min_pmvol=args.min_pmvol,
        min_float=args.min_float,
        max_float=args.max_float,
    )

    if args.csv and not df.empty:
        fname = f"pre_scalp_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(fname, index=False)
        print(f"  Saved → {fname}\n")


if __name__ == "__main__":
    main()
