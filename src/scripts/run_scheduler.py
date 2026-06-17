#!/usr/bin/env python3
"""
scripts.run_scheduler — Morning Routine Scheduler Daemon

Always-on process that runs the post-market morning routine on a fixed daily
schedule (all times Europe/Berlin unless noted):

  02:00–09:00 DE  →  post_market_scan   (= 20:00–03:00 ET — after US market close)
  07:00–09:00 DE  →  generate_prompt_1  (Overnight Thesis Check)
  14:00–15:00 DE  →  generate_prompt_2  (Pre-Market Decision Run)
  16:10–17:10 DE  →  generate_prompt_3  (Opening Confirmation)

Each task fires at most once per day; failed tasks are retried up to 3 times
with a 5-minute gap.  Run state is saved to data/scheduler_runs.json.

Telegram notifications are sent on completion and on final failure.

Usage:
    PYTHONPATH=src python src/scripts/run_scheduler.py
    ./run_scripts/run_scheduler.sh

Environment variables (all optional if not using Telegram):
    TELEGRAM_BOT_TOKEN  or  TELEGRAM_TOKEN
    TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import signal
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
from services.scheduler.models import ScheduledTask
from services.scheduler.scheduler_service import SchedulerService

logger = getLogger(__name__, app_name="scheduler")

_DATA_ROOT   = _ROOT / "data"
_STATUS_FILE = _DATA_ROOT / "scheduler_runs.json"

# ── Morning schedule config ───────────────────────────────────────────────────
# Add more ScheduledTask entries here to extend the scheduler with new routines.

MORNING_SCHEDULE: list[ScheduledTask] = [
    ScheduledTask(
        name         = "post_market_scan",
        task_type    = "scan",
        # 02:00 DE = ~20:00 ET (after US post-market close).
        # Window stays open until 09:00 DE so a missed overnight run is caught
        # in the morning before the prompt-1 window opens.
        window_start = "02:00",
        window_end   = "09:00",
        timezone     = "Europe/Berlin",
        config       = {"scanner": "post_market"},
        max_retries  = 3,
        retry_delay_seconds = 300,
    ),
    ScheduledTask(
        name         = "prompt_1",
        task_type    = "generate_prompt",
        window_start = "07:00",
        window_end   = "09:00",
        timezone     = "Europe/Berlin",
        config       = {"prompt_num": 1},
        max_retries  = 3,
        retry_delay_seconds = 300,
    ),
    ScheduledTask(
        name         = "prompt_2",
        task_type    = "generate_prompt",
        window_start = "14:00",
        window_end   = "15:00",
        timezone     = "Europe/Berlin",
        config       = {"prompt_num": 2},
        max_retries  = 3,
        retry_delay_seconds = 300,
    ),
    ScheduledTask(
        name         = "prompt_3",
        task_type    = "generate_prompt",
        window_start = "16:10",
        window_end   = "17:10",
        timezone     = "Europe/Berlin",
        config       = {"prompt_num": 3},
        max_retries  = 3,
        retry_delay_seconds = 300,
    ),
]


# ── Task handlers ─────────────────────────────────────────────────────────────

async def handle_scan(config: dict, run_date: dt.date) -> str:
    """Run the post-market TradingView scanner and save the daily CSV."""
    scanner_name = config.get("scanner", "post_market")
    if scanner_name != "post_market":
        raise ValueError(f"Unknown scanner: {scanner_name!r}")

    from scripts.scanners.run_post_market_scanner import run_and_save
    _, saved_path = await asyncio.get_event_loop().run_in_executor(None, run_and_save)

    if saved_path is None:
        raise RuntimeError("Scanner returned no results — market may be closed")

    return str(saved_path)


async def handle_generate_prompt(config: dict, run_date: dt.date) -> str:
    """Enrich movers data and write the prompt text file."""
    prompt_num = config.get("prompt_num")
    if prompt_num not in (1, 2, 3):
        raise ValueError(f"config.prompt_num must be 1–3, got {prompt_num!r}")

    svc = MorningEnrichmentService(data_root=_DATA_ROOT)
    out_path = await svc.build_and_save_prompt(
        prompt_num = prompt_num,
        run_date   = run_date,
    )
    return str(out_path)


_HANDLERS = {
    "scan":             handle_scan,
    "generate_prompt":  handle_generate_prompt,
}


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    tg_token   = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    scheduler = SchedulerService(
        tasks        = MORNING_SCHEDULE,
        handlers     = _HANDLERS,
        status_file  = _STATUS_FILE,
        poll_interval = 60,
        telegram_token   = tg_token,
        telegram_chat_id = tg_chat_id,
    )

    loop = asyncio.get_running_loop()

    def _stop(*_):
        logger.info("[Scheduler] received signal — shutting down")
        scheduler.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    if not tg_token or not tg_chat_id:
        logger.warning(
            "[Scheduler] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — "
            "Telegram notifications disabled"
        )

    logger.info("[Scheduler] data root: %s", _DATA_ROOT)
    logger.info("[Scheduler] status file: %s", _STATUS_FILE)
    logger.info("[Scheduler] tasks: %s", [t.name for t in MORNING_SCHEDULE])

    await scheduler.run()


if __name__ == "__main__":
    asyncio.run(main())
