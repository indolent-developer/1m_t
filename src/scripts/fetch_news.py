#!/usr/bin/env python3
"""
Fetch recent news for a ticker from FMP and/or Finnhub.

Setup — add to .env:
    FMP_API_KEY=your_key
    FINNHUB_API_KEY=your_key

Run:
    .penv/bin/python src/scripts/fetch_news.py IREN
    .penv/bin/python src/scripts/fetch_news.py IREN --days 3 --source fmp
    .penv/bin/python src/scripts/fetch_news.py IREN --days 1 --source finnhub
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Load .env
_ROOT = Path(__file__).resolve().parents[2]
_ENV  = _ROOT / ".env"
if _ENV.exists():
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

sys.path.insert(0, str(_ROOT / "src"))


def _print_news(items, source: str) -> int:
    if not items:
        print(f"  [{source}] No news found.\n")
        return 0

    print(f"\n  [{source}] {len(items)} articles\n  {'─'*70}")
    for n in items:
        ts = n.published_date.strftime("%Y-%m-%d %H:%M") if n.published_date else "?"
        print(f"  {ts}  {n.publisher or n.site}")
        print(f"  {n.title}")
        print(f"  {n.url}")
        print()
    return len(items)


def run(ticker: str, days: int, source: str) -> None:
    ticker    = ticker.upper()
    to_date   = date.today()
    from_date = to_date - timedelta(days=days)

    print(f"\n  NEWS: {ticker}  |  {from_date} → {to_date}  |  source: {source}")
    print(f"  {'═'*70}\n")

    if source in ("fmp", "all"):
        try:
            from infrastructure.gateways.market_data.fmp_client import FMPClient
            client = FMPClient()
            news   = client.get_stock_news(ticker, limit=20, from_date=from_date, to_date=to_date)
            _print_news(news, "FMP")

            grades = client.get_analyst_grades(ticker, limit=5)
            if grades:
                print(f"  [FMP] Analyst grades ({len(grades)})\n  {'─'*70}")
                for g in grades:
                    print(f"  {g.date}  {g.grading_company}  "
                          f"{g.previous_grade.value if g.previous_grade else '?'} → "
                          f"{g.new_grade.value if g.new_grade else '?'}  [{g.action}]")
                print()
        except ValueError as e:
            print(f"  [FMP] Skipped — {e}\n")
        except Exception as e:
            print(f"  [FMP] Error — {e}\n")

    if source in ("finnhub", "all"):
        try:
            from infrastructure.gateways.market_data.finnhub_client import FinnhubClient
            client = FinnhubClient()
            news   = client.get_company_news(ticker, from_date=from_date, to_date=to_date)
            _print_news(news, "Finnhub")

            sentiment = client.get_news_sentiment(ticker)
            if sentiment.get("buzz"):
                buzz  = sentiment["buzz"]
                score = sentiment.get("companyNewsScore", "?")
                print(f"  [Finnhub] Sentiment: score={score}  "
                      f"articles={buzz.get('articlesInLastWeek', '?')}  "
                      f"buzz={buzz.get('buzz', '?')}\n")
        except ValueError as e:
            print(f"  [Finnhub] Skipped — {e}\n")
        except Exception as e:
            print(f"  [Finnhub] Error — {e}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch stock news")
    parser.add_argument("ticker",             help="Stock ticker (e.g. IREN)")
    parser.add_argument("--days",   type=int, default=2,    help="Days back to search (default 2)")
    parser.add_argument("--source", choices=["fmp", "finnhub", "all"], default="all",
                        help="Data source (default: all)")
    args = parser.parse_args()
    run(args.ticker, args.days, args.source)


if __name__ == "__main__":
    main()
