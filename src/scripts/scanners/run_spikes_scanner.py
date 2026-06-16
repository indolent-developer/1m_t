#!/usr/bin/env python3
"""
Intraday Spikes Scanner
Replicates TradingView "nk-spikes" screener:
  - US stocks
  - Price > $2
  - Market cap > $300M
  - Avg vol 30D > 500K
  - Chg from open OUTSIDE -2% to +2%
  - Rel vol > 4

API notes:
  change_from_open          — % move from today's open (closest available proxy
                              to TradingView's "Chg 5m"; intraday rolling 5m
                              is not exposed in the screener API)
  relative_volume_10d_calc  — today's vol vs 10-day avg full-day vol
                              (TradingView's "Rel vol at time" is not in the API;
                              this is the best available equivalent)

Run:
  python run_spikes_scanner.py
  python run_spikes_scanner.py --min-chg 3 --min-relvol 6 --limit 30
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

DEFAULT_LIMIT      = 50
DEFAULT_MIN_CHG    = 2.0    # abs intraday change threshold (%)
DEFAULT_MIN_RELVOL = 4.0    # rel vol at time-of-day threshold
DEFAULT_MIN_PRICE  = 2.0
DEFAULT_MIN_MKTCAP = 300_000_000
DEFAULT_MIN_AVGVOL = 500_000


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
    min_chg=DEFAULT_MIN_CHG,
    min_relvol=DEFAULT_MIN_RELVOL,
    min_price=DEFAULT_MIN_PRICE,
    min_mktcap=DEFAULT_MIN_MKTCAP,
    min_avgvol=DEFAULT_MIN_AVGVOL,
):
    print(f"\n{'─'*65}")
    print(f"  INTRADAY SPIKES SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'─'*65}")
    print(f"  Filters: Price>${min_price} | MCap>{fmt_mktcap(min_mktcap)} | "
          f"AvgVol>{fmt_vol(min_avgvol)}")
    print(f"  Spike:   |Chg|>{min_chg}% from open  |  Rel vol@time >{min_relvol}x")
    print(f"{'─'*65}\n")

    COLS = [
        "name", "description", "close", "change", "change_from_open",
        "volume", "relative_volume_10d_calc",
        "market_cap_basic", "average_volume_30d_calc", "sector",
    ]
    BASE_FILTERS = [
        col("close") > min_price,
        col("market_cap_basic") > min_mktcap,
        col("average_volume_30d_calc") > min_avgvol,
        col("relative_volume_10d_calc") > min_relvol,
    ]

    def _query(chg_filter):
        _, frame = (
            Query()
            .set_markets("america")
            .select(*COLS)
            .where(*BASE_FILTERS, chg_filter)
            .order_by("relative_volume_10d_calc", ascending=False)
            .limit(500)
            .get_scanner_data()
        )
        return frame

    df = pd.concat([
        _query(col("change_from_open") >  min_chg),
        _query(col("change_from_open") < -min_chg),
    ], ignore_index=True).drop_duplicates(subset="name")

    df = df.sort_values("change_from_open", ascending=False).head(limit)

    if df.empty:
        print("  No spikes found. Market may be closed or pre-open.\n")
        return df

    gainers = df[df["change_from_open"] > 0].copy()
    losers  = df[df["change_from_open"] < 0].copy()

    def print_section(subset, label):
        if subset.empty:
            return
        print(f"\n  {label} ({len(subset)})")
        print(f"  {'─'*120}")
        print(f"  {'#':<3} {'TICKER':<8} {'NAME':<26} {'PRICE':>7} {'DAY CHG':>8} "
              f"{'CHG/OPEN':>9} {'REL VOL':>8} {'VOLUME':>9} {'MKTCAP':>10}  SECTOR")
        print(f"  {'─'*115}")
        for i, (_, row) in enumerate(subset.iterrows(), 1):
            chg_open = row["change_from_open"]
            relvol   = row.get("relative_volume_10d_calc", 0) or 0
            name     = str(row.get("description", ""))[:24]
            print(
                f"  {i:<3} {row['name']:<8} {name:<26} "
                f"${row['close']:>6.2f} "
                f"{row['change']:>+7.2f}% "
                f"{arrow(chg_open)}{abs(chg_open):>7.2f}% "
                f"{relvol:>7.2f}x "
                f"{fmt_vol(row['volume']):>9} "
                f"{fmt_mktcap(row['market_cap_basic']):>10}  "
                f"{row.get('sector', '')}"
            )

    print_section(gainers, "🟢 SPIKING UP")
    print_section(losers,  "🔴 SPIKING DOWN")

    print(f"\n  {'─'*65}")
    print(f"  Total: {len(df)} results  |  Up: {len(gainers)}  |  Down: {len(losers)}")
    print(f"  {'─'*65}\n")

    return df


def main():
    parser = argparse.ArgumentParser(description="Intraday Spikes Scanner")
    parser.add_argument("--limit",      type=int,   default=DEFAULT_LIMIT,      help="Max results (default 50)")
    parser.add_argument("--min-chg",    type=float, default=DEFAULT_MIN_CHG,    help="Min |chg from open| %% (default 2)")
    parser.add_argument("--min-relvol", type=float, default=DEFAULT_MIN_RELVOL, help="Min rel vol at time (default 4)")
    parser.add_argument("--min-price",  type=float, default=DEFAULT_MIN_PRICE,  help="Min price (default 2)")
    parser.add_argument("--min-mktcap", type=float, default=DEFAULT_MIN_MKTCAP, help="Min mkt cap (default 300M)")
    parser.add_argument("--min-avgvol", type=float, default=DEFAULT_MIN_AVGVOL, help="Min avg vol 30D (default 500K)")
    parser.add_argument("--csv",        action="store_true",                    help="Save results to CSV")
    args = parser.parse_args()

    df = run_scanner(
        limit=args.limit,
        min_chg=args.min_chg,
        min_relvol=args.min_relvol,
        min_price=args.min_price,
        min_mktcap=args.min_mktcap,
        min_avgvol=args.min_avgvol,
    )

    if args.csv and not df.empty:
        fname = f"spikes_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(fname, index=False)
        print(f"  Saved → {fname}\n")


if __name__ == "__main__":
    main()
