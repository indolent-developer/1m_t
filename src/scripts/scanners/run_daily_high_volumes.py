#!/usr/bin/env python3
"""
Daily High Volumes Scanner
Replicates TradingView "nk-daily-high-volumes" screener:
  - US stocks
  - Price > $2
  - Market cap > $300M
  - Avg vol 30D > 500K
  - Rel vol > threshold

Threshold modes:
  --smart        Dynamic: scales 1.0→3.0 linearly over the trading day (9:30→4:00 ET)
  (default)      Fixed at 3.0, or at --relvol N if provided

Run:
  python run_daily_high_volumes.py              # fixed 3.0
  python run_daily_high_volumes.py --relvol 2   # fixed 2.0
  python run_daily_high_volumes.py --smart      # dynamic
"""

import argparse
import sys
from datetime import datetime

try:
    import pytz
    from tradingview_screener import Query, col
    import pandas as pd
except ImportError:
    print("Missing deps. Run:  pip install tradingview-screener pandas tabulate pytz")
    sys.exit(1)

DEFAULT_LIMIT      = 50
DEFAULT_MIN_PRICE  = 2.0
DEFAULT_MIN_MKTCAP = 300_000_000
DEFAULT_MIN_AVGVOL = 500_000

MARKET_OPEN_H,  MARKET_OPEN_M  = 9,  30
MARKET_CLOSE_H, MARKET_CLOSE_M = 16, 0
MARKET_MINUTES = 390   # 6.5-hour session

SMART_MIN   = 1.0
SMART_MAX   = 3.0
DEFAULT_REL = 3.0


def smart_threshold() -> float:
    """Linear ramp 1.0→3.0 from open to close in ET."""
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
    close_ = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)

    if now <= open_:
        return SMART_MIN
    if now >= close_:
        return SMART_MAX

    elapsed = (now - open_).total_seconds() / 60
    return SMART_MIN + (SMART_MAX - SMART_MIN) * (elapsed / MARKET_MINUTES)


def fmt_mktcap(v):
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    return f"${v/1e6:.0f}M"

def fmt_vol(v):
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(int(v))


def run_scanner(
    min_relvol,
    mode_label,
    limit=DEFAULT_LIMIT,
    min_price=DEFAULT_MIN_PRICE,
    min_mktcap=DEFAULT_MIN_MKTCAP,
    min_avgvol=DEFAULT_MIN_AVGVOL,
):
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et).strftime("%Y-%m-%d %H:%M ET")

    print(f"\n{'─'*65}")
    print(f"  DAILY HIGH VOLUMES SCANNER  |  {now_et}")
    print(f"{'─'*65}")
    print(f"  Mode: {mode_label}  |  Rel Vol > {min_relvol:.2f}x")
    print(f"  Filters: Price>${min_price} | MCap>{fmt_mktcap(min_mktcap)} | AvgVol>{fmt_vol(min_avgvol)}")
    print(f"{'─'*65}\n")

    _, df = (
        Query()
        .set_markets("america")
        .select(
            "name",
            "description",
            "close",
            "change",
            "volume",
            "relative_volume_10d_calc",
            "market_cap_basic",
            "average_volume_30d_calc",
            "sector",
        )
        .where(
            col("close") > min_price,
            col("market_cap_basic") > min_mktcap,
            col("average_volume_30d_calc") > min_avgvol,
            col("relative_volume_10d_calc") > min_relvol,
        )
        .order_by("relative_volume_10d_calc", ascending=False)
        .limit(limit)
        .get_scanner_data()
    )

    if df.empty:
        print("  No results found.\n")
        return df

    gainers = df[df["change"] > 0].copy()
    losers  = df[df["change"] <= 0].copy()

    def print_section(subset, label):
        if subset.empty:
            return
        print(f"\n  {label} ({len(subset)})")
        print(f"  {'─'*115}")
        print(f"  {'#':<3} {'TICKER':<8} {'NAME':<28} {'PRICE':>7} {'CHG':>8} {'REL VOL':>8} "
              f"{'VOLUME':>9} {'AVG VOL':>9} {'MKTCAP':>10}  SECTOR")
        print(f"  {'─'*115}")
        for i, (_, row) in enumerate(subset.iterrows(), 1):
            chg  = row["change"]
            name = str(row.get("description", ""))[:26]
            print(
                f"  {i:<3} {row['name']:<8} {name:<28} "
                f"${row['close']:>6.2f} "
                f"{chg:>+7.2f}% "
                f"{row['relative_volume_10d_calc']:>7.2f}x "
                f"{fmt_vol(row['volume']):>9} "
                f"{fmt_vol(row['average_volume_30d_calc']):>9} "
                f"{fmt_mktcap(row['market_cap_basic']):>10}  "
                f"{row.get('sector','')}"
            )

    print_section(gainers, "🟢 UP")
    print_section(losers,  "🔴 DOWN")

    print(f"\n  {'─'*65}")
    print(f"  Total: {len(df)} results  |  Up: {len(gainers)}  |  Down: {len(losers)}")
    print(f"  {'─'*65}\n")

    return df


def main():
    parser = argparse.ArgumentParser(description="Daily High Volumes Scanner")
    parser.add_argument("--smart",      action="store_true",
                        help="Dynamic threshold: 1.0→3.0 scaled to time of day")
    parser.add_argument("--relvol",     type=float, metavar="N",
                        help=f"Fixed threshold (default {DEFAULT_REL})")
    parser.add_argument("--limit",      type=int,   default=DEFAULT_LIMIT,     help="Max results (default 50)")
    parser.add_argument("--min-price",  type=float, default=DEFAULT_MIN_PRICE, help="Min price (default 2)")
    parser.add_argument("--min-mktcap", type=float, default=DEFAULT_MIN_MKTCAP,help="Min mkt cap (default 300M)")
    parser.add_argument("--min-avgvol", type=float, default=DEFAULT_MIN_AVGVOL,help="Min avg vol 30D (default 500K)")
    parser.add_argument("--csv",        action="store_true",                   help="Save results to CSV")
    args = parser.parse_args()

    # Resolve threshold and label
    if args.smart:
        min_relvol  = smart_threshold()
        et          = pytz.timezone("America/New_York")
        now_et      = datetime.now(et)
        open_       = now_et.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
        elapsed_pct = min(100, max(0, (now_et - open_).total_seconds() / (MARKET_MINUTES * 60) * 100))
        mode_label  = f"SMART ({elapsed_pct:.0f}% of session)"
    else:
        min_relvol  = args.relvol if args.relvol is not None else DEFAULT_REL
        mode_label  = f"FIXED ({min_relvol:.2f}x)"

    df = run_scanner(
        min_relvol=min_relvol,
        mode_label=mode_label,
        limit=args.limit,
        min_price=args.min_price,
        min_mktcap=args.min_mktcap,
        min_avgvol=args.min_avgvol,
    )

    if args.csv and not df.empty:
        fname = f"high_vol_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(fname, index=False)
        print(f"  Saved → {fname}\n")


if __name__ == "__main__":
    main()
