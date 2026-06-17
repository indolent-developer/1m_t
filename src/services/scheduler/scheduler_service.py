"""
services.scheduler.scheduler_service — SchedulerService

Generic always-on async scheduler.  Tasks are defined via ScheduledTask objects;
execution is delegated to injected handler callables keyed by task_type.

Behaviour per tick (every poll_interval seconds):
  For each task:
    1. Skip if current time is outside the task's daily window.
    2. Skip if today's run already succeeded (idempotent / run-once per day).
    3. Skip if max_retries exhausted for today.
    4. Skip if last failure was too recent (retry_delay_seconds not elapsed).
    5. Otherwise execute the handler.
  On success → status = "success", Telegram notification sent.
  On failure → status = "failed", will retry per above rules.

Run state is persisted to a JSON file after every attempt so restarts are safe.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from core.utils.log_helper import getLogger

from . import run_status as rs
from .models import ScheduledTask, TaskRunRecord

logger = getLogger(__name__)

# Handler signature: async (config: dict, date: datetime.date) -> str (output_file path)
TaskHandler = Callable[[dict[str, Any], Any], Awaitable[str]]


class SchedulerService:
    """
    Generic always-on daily-task scheduler.

    handlers:
        Maps task_type string → async callable.
        Signature: async (config: dict, run_date: datetime.date) -> str
        Must return the path of the produced output file (used for logging +
        Telegram notification).  Raise any exception to signal failure.

    status_file:
        Path to the JSON run-status file (e.g. data/scheduler_runs.json).
        Created on first write.  Pruned to the last 30 days on startup.

    poll_interval:
        Seconds between window-check ticks.  Default 60 s.
    """

    def __init__(
        self,
        tasks: list[ScheduledTask],
        handlers: dict[str, TaskHandler],
        status_file: Path,
        poll_interval: int = 60,
        telegram_token: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self._tasks      = tasks
        self._handlers   = handlers
        self._status_file = status_file
        self._poll       = poll_interval
        self._tg_token   = telegram_token
        self._tg_chat_id = telegram_chat_id
        self._state: dict = {}
        self._running = False

    # ── Public ────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._state = rs.load(self._status_file)
        rs.prune_old_dates(self._state)
        rs.save(self._status_file, self._state)

        self._running = True
        logger.info(
            "[Scheduler] started  tasks=%d  poll=%ds  status=%s",
            len(self._tasks), self._poll, self._status_file,
        )

        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("[Scheduler] unexpected error in tick")
            await asyncio.sleep(self._poll)

    def stop(self) -> None:
        self._running = False
        logger.info("[Scheduler] stopping")

    # ── Internal tick ─────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        for task in self._tasks:
            tz  = ZoneInfo(task.timezone)
            now = datetime.now(tz)
            date_key = now.strftime("%Y-%m-%d")

            if not self._in_window(task, now):
                continue

            record = rs.get_record(self._state, date_key, task.name)
            if record is None:
                record = TaskRunRecord(status="pending")

            if record.status == "success":
                continue

            if record.status == "running":
                # Guard against a stuck "running" state after a crash.
                # If it was marked running more than 30 minutes ago, reset it.
                if record.last_run_at:
                    last = datetime.fromisoformat(record.last_run_at)
                    if (now - last).total_seconds() > 1800:
                        logger.warning(
                            "[Scheduler] %s stuck in 'running' for >30 min — resetting to failed",
                            task.name,
                        )
                        record.status = "failed"
                    else:
                        continue
                else:
                    continue

            if record.attempts >= task.max_retries:
                continue

            if record.status == "failed" and record.last_run_at:
                last = datetime.fromisoformat(record.last_run_at)
                elapsed = (now - last).total_seconds()
                if elapsed < task.retry_delay_seconds:
                    logger.debug(
                        "[Scheduler] %s waiting for retry  elapsed=%.0fs  delay=%ds",
                        task.name, elapsed, task.retry_delay_seconds,
                    )
                    continue

            await self._execute(task, date_key, record, now)

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute(
        self,
        task: ScheduledTask,
        date_key: str,
        record: TaskRunRecord,
        now: datetime,
    ) -> None:
        record.status      = "running"
        record.attempts   += 1
        record.last_run_at = now.isoformat()
        record.error       = None
        rs.set_record(self._state, date_key, task.name, record)
        rs.save(self._status_file, self._state)

        logger.info(
            "[Scheduler] → %s  attempt=%d/%d",
            task.name, record.attempts, task.max_retries,
        )

        handler = self._handlers.get(task.task_type)
        if handler is None:
            record.status = "failed"
            record.error  = f"no handler registered for task_type={task.task_type!r}"
            rs.set_record(self._state, date_key, task.name, record)
            rs.save(self._status_file, self._state)
            logger.error("[Scheduler] %s — %s", task.name, record.error)
            return

        try:
            output_file = await handler(task.config, now.date())
            record.status      = "success"
            record.output_file = str(output_file)
            logger.info("[Scheduler] ✓ %s → %s", task.name, output_file)
            await self._notify_success(task.name, str(output_file))
        except Exception as exc:
            record.status = "failed"
            record.error  = str(exc)
            logger.exception("[Scheduler] ✗ %s — %s", task.name, exc)
            await self._notify_failure(task.name, str(exc), record.attempts, task.max_retries)

        rs.set_record(self._state, date_key, task.name, record)
        rs.save(self._status_file, self._state)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _in_window(task: ScheduledTask, now: datetime) -> bool:
        ws_h, ws_m = map(int, task.window_start.split(":"))
        we_h, we_m = map(int, task.window_end.split(":"))
        now_mins = now.hour * 60 + now.minute
        start_mins = ws_h * 60 + ws_m
        end_mins   = we_h * 60 + we_m
        return start_mins <= now_mins <= end_mins

    async def _notify_success(self, task_name: str, output_file: str) -> None:
        if not self._tg_token or not self._tg_chat_id:
            return
        msg = f"✅ *{task_name}* complete\n`{output_file}`"
        await self._tg_send(msg)

    async def _notify_failure(
        self, task_name: str, error: str, attempt: int, max_retries: int
    ) -> None:
        if not self._tg_token or not self._tg_chat_id:
            return
        retrying = attempt < max_retries
        status = f"retrying ({attempt}/{max_retries})" if retrying else "EXHAUSTED — no more retries today"
        msg = f"⚠️ *{task_name}* failed — {status}\n`{error[:200]}`"
        await self._tg_send(msg)

    async def _tg_send(self, text: str) -> None:
        try:
            from telegram import Bot
            bot = Bot(token=self._tg_token)
            await bot.send_message(
                chat_id=self._tg_chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("[Scheduler] Telegram send failed")
