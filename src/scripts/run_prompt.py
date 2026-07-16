#!/usr/bin/env python3
"""
scripts.run_prompt — On-demand prompt generation

Generates one of the three morning prompts immediately, bypassing the scheduler
window check. Useful for manual runs and testing.

  Prompt 1 — Overnight Thesis Check       (normally 07:00 DE / 01:00 ET)
  Prompt 2 — Pre-Market Decision Run      (normally 14:00 DE / 08:00 ET)
  Prompt 3 — Opening Confirmation         (normally 16:10 DE / 10:05 ET)

Usage:
    ./run_scripts/run_prompt.sh           # defaults to prompt 2
    ./run_scripts/run_prompt.sh --prompt 1
    ./run_scripts/run_prompt.sh --prompt 3
    ./run_scripts/run_prompt.sh --prompt 2 --date 2026-06-17
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_SRC  = Path(__file__).resolve().parents[2] / "src"
_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_ENV = _ROOT / ".env"
if _ENV.exists():
    try:
        from dotenv import dotenv_values
        for k, v in dotenv_values(_ENV).items():
            os.environ.setdefault(k, v or "")
    except ImportError:
        for line in _ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
# ─────────────────────────────────────────────────────────────────────────────

from core.utils.log_helper import getLogger
from services.morning_enrichment import MorningEnrichmentService

logger = getLogger(__name__, app_name="run_prompt")

_DATA_ROOT = _ROOT / "data"


def _build_price_history_svc():
    """Wire up FMP + Redis → PriceHistoryService. Returns None if FMP_API_KEY missing."""
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        logger.warning("FMP_API_KEY not set — intraday 5-min data will be skipped for prompt 3")
        return None
    from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
    from infrastructure.cache.redis_cache import RedisCache
    from services.price_history_service import PriceHistoryService
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return PriceHistoryService(
        fetcher=FmpDataFetcher({"api_key": fmp_key}),
        cache=RedisCache(url=redis_url),
        fetcher_name="fmp",
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a morning prompt on demand")
    parser.add_argument(
        "prompt_pos",
        type=int,
        choices=[1, 2, 3],
        nargs="?",
        default=None,
        metavar="PROMPT",
        help="Prompt number (positional): 1, 2, or 3",
    )
    parser.add_argument(
        "--prompt", "-p",
        type=int,
        choices=[1, 2, 3],
        default=2,
        help="Prompt number to generate: 1=Overnight, 2=Pre-Market (default), 3=Opening",
    )
    parser.add_argument(
        "--date", "-d",
        default=None,
        help="Run date as YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args()
    prompt_num = args.prompt_pos if args.prompt_pos is not None else args.prompt

    run_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    labels = {1: "Overnight Thesis Check", 2: "Pre-Market Decision Run", 3: "Opening Confirmation"}
    logger.info("Generating prompt %d — %s  (date=%s)", prompt_num, labels[prompt_num], run_date)

    price_history_svc = _build_price_history_svc() if prompt_num == 3 else None

    from services.news_service import NewsService
    news_svc = NewsService(lookback_days=2)

    svc = MorningEnrichmentService(data_root=_DATA_ROOT, price_history_svc=price_history_svc, news_svc=news_svc)
    out_path = await svc.build_and_save_prompt(
        prompt_num=prompt_num,
        run_date=run_date,
    )

    print(f"\n  Saved → {out_path}")
    try:
        import subprocess
        subprocess.run(["pbcopy"], input=out_path.read_bytes(), check=False)
        print("  → prompt copied to clipboard\n")
    except Exception:
        print()


if __name__ == "__main__":
    asyncio.run(main())
