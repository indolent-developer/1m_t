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

Install: pip install tradingview-screener pandas tabulate
Run:     python pm_movers_scanner.py
         python pm_movers_scanner.py --limit 30 --min-pmchg 5
"""

import argparse
import sys
from datetime import datetime

try:
    from tradingview_screener import Query, col
    import pandas as pd
except ImportError:
    print("Missing deps. Run:  pip install tradingview-screener pandas tabulate")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_LIMIT       = 50
DEFAULT_MIN_PMCHG   = 3.0   # abs post-mkt change threshold (%)
DEFAULT_MIN_PRICE   = 2.0
DEFAULT_MIN_MKTCAP  = 300_000_000
DEFAULT_MIN_AVGVOL  = 500_000
DEFAULT_MIN_PMVOL   = 100_000
# ────────────────────────────────────────────────────────────────────────────


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
):
    print(f"\n{'─'*60}")
    print(f"  POST-MARKET MOVERS SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'─'*60}")
    print(f"  Filters: Price>${min_price} | MCap>${fmt_mktcap(min_mktcap)} | "
          f"AvgVol>{fmt_vol(min_avgvol)} | |PM Chg|>{min_pmchg}% | PM Vol>{fmt_vol(min_pmvol)}")
    print(f"{'─'*60}\n")

    COLS = [
        "name", "description", "close", "change", "volume",
        "market_cap_basic", "average_volume_30d_calc",
        "postmarket_change", "postmarket_volume", "sector", "type",
    ]
    BASE_FILTERS = [
        col("close") > min_price,
        col("market_cap_basic") > min_mktcap,
        col("average_volume_30d_calc") > min_avgvol,
        col("postmarket_volume") > min_pmvol,
    ]

    # Two server-side queries mirror TradingView "outside -3% to 3%"
    def _query(extra_filter):
        _, frame = (
            Query()
            .set_markets("america")
            .select(*COLS)
            .where(*BASE_FILTERS, extra_filter)
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

    # ── Gainers ──────────────────────────────────────────────────────────
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


def main():
    parser = argparse.ArgumentParser(description="Post-Market Movers Scanner")
    parser.add_argument("--limit",      type=int,   default=DEFAULT_LIMIT,     help="Max results (default 50)")
    parser.add_argument("--min-pmchg",  type=float, default=DEFAULT_MIN_PMCHG, help="Min |PM change| %% (default 3)")
    parser.add_argument("--min-price",  type=float, default=DEFAULT_MIN_PRICE, help="Min price (default 2)")
    parser.add_argument("--min-mktcap", type=float, default=DEFAULT_MIN_MKTCAP,help="Min mkt cap (default 300M)")
    parser.add_argument("--min-avgvol", type=float, default=DEFAULT_MIN_AVGVOL,help="Min avg vol 30D (default 500K)")
    parser.add_argument("--min-pmvol",  type=float, default=DEFAULT_MIN_PMVOL, help="Min PM volume (default 100K)")
    parser.add_argument("--csv",        action="store_true",                   help="Also save results to CSV")
    args = parser.parse_args()

    df = run_scanner(
        limit=args.limit,
        min_pmchg=args.min_pmchg,
        min_price=args.min_price,
        min_mktcap=args.min_mktcap,
        min_avgvol=args.min_avgvol,
        min_pmvol=args.min_pmvol,
    )

    if args.csv and not df.empty:
        fname = f"pm_movers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(fname, index=False)
        print(f"  Saved → {fname}\n")


if __name__ == "__main__":
    main()