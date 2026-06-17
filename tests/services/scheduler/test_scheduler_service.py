"""
Tests for services.scheduler.scheduler_service.SchedulerService

No real async I/O — handlers and Telegram are mocked.
Time is controlled by patching datetime.now inside the module.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from services.scheduler.models import ScheduledTask, TaskRunRecord
from services.scheduler.scheduler_service import SchedulerService
from services.scheduler import run_status as rs

_TZ = "Europe/Berlin"
_BERLIN = ZoneInfo(_TZ)


# ── helpers ───────────────────────────────────────────────────────────────────

def _task(
    name="scan",
    task_type="scan",
    window_start="07:00",
    window_end="09:00",
    timezone=_TZ,
    config=None,
    max_retries=3,
    retry_delay_seconds=300,
) -> ScheduledTask:
    return ScheduledTask(
        name=name,
        task_type=task_type,
        window_start=window_start,
        window_end=window_end,
        timezone=timezone,
        config=config or {},
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
    )


def _svc(
    tasks=None,
    handlers=None,
    status_file=None,
    telegram_token="",
    telegram_chat_id="",
    tmp_path=None,
) -> SchedulerService:
    if status_file is None:
        import tempfile, pathlib
        status_file = pathlib.Path(tempfile.mktemp(suffix=".json"))
    return SchedulerService(
        tasks=tasks or [],
        handlers=handlers or {},
        status_file=status_file,
        poll_interval=60,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
    )


def _now(hour: int, minute: int = 0) -> dt.datetime:
    """Return a Berlin-timezone datetime on a fixed date at given hour:minute."""
    return dt.datetime(2026, 6, 17, hour, minute, 0, tzinfo=_BERLIN)


# ── _in_window ────────────────────────────────────────────────────────────────

def test_in_window_returns_true_at_start():
    task = _task(window_start="07:00", window_end="09:00")
    assert SchedulerService._in_window(task, _now(7, 0)) is True


def test_in_window_returns_true_in_middle():
    task = _task(window_start="07:00", window_end="09:00")
    assert SchedulerService._in_window(task, _now(8, 0)) is True


def test_in_window_returns_true_at_end():
    task = _task(window_start="07:00", window_end="09:00")
    assert SchedulerService._in_window(task, _now(9, 0)) is True


def test_in_window_returns_false_before_start():
    task = _task(window_start="07:00", window_end="09:00")
    assert SchedulerService._in_window(task, _now(6, 59)) is False


def test_in_window_returns_false_after_end():
    task = _task(window_start="07:00", window_end="09:00")
    assert SchedulerService._in_window(task, _now(9, 1)) is False


def test_in_window_one_minute_window():
    task = _task(window_start="14:00", window_end="14:00")
    assert SchedulerService._in_window(task, _now(14, 0)) is True
    assert SchedulerService._in_window(task, _now(14, 1)) is False


# ── _execute — success path ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_calls_handler_with_config_and_date(tmp_path):
    captured = {}
    async def handler(config, run_date):
        captured["config"]    = config
        captured["run_date"]  = run_date
        return "/output/file.txt"

    task = _task(name="t1", task_type="h", config={"k": "v"})
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    record = TaskRunRecord(status="pending")
    now    = _now(7, 5)

    await svc._execute(task, "2026-06-17", record, now)

    assert captured["config"]   == {"k": "v"}
    assert captured["run_date"] == now.date()


@pytest.mark.asyncio
async def test_execute_sets_status_success_on_handler_return(tmp_path):
    async def handler(config, run_date): return "/out.txt"

    task = _task(name="t1", task_type="h")
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    record = TaskRunRecord(status="pending")

    await svc._execute(task, "2026-06-17", record, _now(7))

    assert record.status == "success"
    assert record.output_file == "/out.txt"
    assert record.error is None


@pytest.mark.asyncio
async def test_execute_increments_attempts(tmp_path):
    async def handler(config, run_date): return "/out.txt"

    task   = _task(task_type="h")
    svc    = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    record = TaskRunRecord(status="pending", attempts=1)

    await svc._execute(task, "2026-06-17", record, _now(7))

    assert record.attempts == 2


@pytest.mark.asyncio
async def test_execute_persists_state_to_json(tmp_path):
    async def handler(config, run_date): return "/out.txt"

    status_file = tmp_path / "runs.json"
    task        = _task(name="scan", task_type="h")
    svc         = _svc(tasks=[task], handlers={"h": handler}, status_file=status_file)
    svc._state  = {}

    await svc._execute(task, "2026-06-17", TaskRunRecord(status="pending"), _now(7))

    data = rs.load(status_file)
    assert data["2026-06-17"]["scan"]["status"] == "success"


# ── _execute — failure path ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_sets_status_failed_on_exception(tmp_path):
    async def handler(config, run_date): raise RuntimeError("boom")

    task   = _task(task_type="h")
    svc    = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    record = TaskRunRecord(status="pending")

    await svc._execute(task, "2026-06-17", record, _now(7))

    assert record.status == "failed"
    assert "boom" in record.error
    assert record.output_file is None


@pytest.mark.asyncio
async def test_execute_failed_on_missing_handler(tmp_path):
    task   = _task(task_type="unknown_type")
    svc    = _svc(tasks=[task], handlers={}, status_file=tmp_path / "runs.json")
    svc._state = {}
    record = TaskRunRecord(status="pending")

    await svc._execute(task, "2026-06-17", record, _now(7))

    assert record.status == "failed"
    assert "no handler" in record.error


@pytest.mark.asyncio
async def test_execute_stores_error_message(tmp_path):
    async def handler(config, run_date): raise ValueError("CSV not found")

    task   = _task(task_type="h")
    svc    = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    record = TaskRunRecord(status="pending")

    await svc._execute(task, "2026-06-17", record, _now(7))

    assert "CSV not found" in record.error


# ── _tick — skip conditions ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tick_skips_task_outside_window(tmp_path):
    called = []
    async def handler(config, run_date):
        called.append(True)
        return "/out"

    task = _task(window_start="07:00", window_end="09:00", task_type="h")
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}

    fixed_now = dt.datetime(2026, 6, 17, 10, 0, 0, tzinfo=_BERLIN)  # outside window
    with patch("services.scheduler.scheduler_service.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = dt.datetime.fromisoformat
        await svc._tick()

    assert called == []


@pytest.mark.asyncio
async def test_tick_skips_already_succeeded_task(tmp_path):
    called = []
    async def handler(config, run_date):
        called.append(True)
        return "/out"

    date_key = "2026-06-17"
    task = _task(window_start="07:00", window_end="09:00", task_type="h")
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    rs.set_record(svc._state, date_key, task.name, TaskRunRecord(status="success"))

    fixed_now = dt.datetime(2026, 6, 17, 7, 30, 0, tzinfo=_BERLIN)
    with patch("services.scheduler.scheduler_service.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = dt.datetime.fromisoformat
        await svc._tick()

    assert called == []


@pytest.mark.asyncio
async def test_tick_skips_when_max_retries_exhausted(tmp_path):
    called = []
    async def handler(config, run_date):
        called.append(True)
        return "/out"

    date_key = "2026-06-17"
    task = _task(window_start="07:00", window_end="09:00", task_type="h", max_retries=2)
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    rs.set_record(svc._state, date_key, task.name,
                  TaskRunRecord(status="failed", attempts=2))

    fixed_now = dt.datetime(2026, 6, 17, 7, 30, 0, tzinfo=_BERLIN)
    with patch("services.scheduler.scheduler_service.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = dt.datetime.fromisoformat
        await svc._tick()

    assert called == []


@pytest.mark.asyncio
async def test_tick_skips_when_retry_delay_not_elapsed(tmp_path):
    called = []
    async def handler(config, run_date):
        called.append(True)
        return "/out"

    date_key = "2026-06-17"
    # Last run was 1 minute ago; delay is 300 s
    last_run = dt.datetime(2026, 6, 17, 7, 29, 0, tzinfo=_BERLIN)
    task = _task(window_start="07:00", window_end="09:00", task_type="h",
                 retry_delay_seconds=300)
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    rs.set_record(svc._state, date_key, task.name,
                  TaskRunRecord(status="failed", attempts=1,
                                last_run_at=last_run.isoformat()))

    fixed_now = dt.datetime(2026, 6, 17, 7, 30, 0, tzinfo=_BERLIN)
    with patch("services.scheduler.scheduler_service.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = dt.datetime.fromisoformat
        await svc._tick()

    assert called == []


@pytest.mark.asyncio
async def test_tick_executes_pending_task_in_window(tmp_path):
    called = []
    async def handler(config, run_date):
        called.append(True)
        return "/out"

    task = _task(window_start="07:00", window_end="09:00", task_type="h")
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}

    fixed_now = dt.datetime(2026, 6, 17, 7, 30, 0, tzinfo=_BERLIN)
    with patch("services.scheduler.scheduler_service.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = dt.datetime.fromisoformat
        await svc._tick()

    assert called == [True]


# ── stuck-running reset ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tick_resets_stuck_running_after_30_min(tmp_path):
    """A task stuck in 'running' for >30 min should be reset to failed and retried."""
    called = []
    async def handler(config, run_date):
        called.append(True)
        return "/out"

    date_key = "2026-06-17"
    stuck_at = dt.datetime(2026, 6, 17, 6, 55, 0, tzinfo=_BERLIN)  # 35 min ago
    task = _task(window_start="07:00", window_end="09:00", task_type="h")
    svc  = _svc(tasks=[task], handlers={"h": handler}, status_file=tmp_path / "runs.json")
    svc._state = {}
    rs.set_record(svc._state, date_key, task.name,
                  TaskRunRecord(status="running", attempts=1,
                                last_run_at=stuck_at.isoformat()))

    fixed_now = dt.datetime(2026, 6, 17, 7, 30, 0, tzinfo=_BERLIN)
    with patch("services.scheduler.scheduler_service.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat.side_effect = dt.datetime.fromisoformat
        await svc._tick()

    assert called == [True]


# ── Telegram notifications ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_success_sends_telegram(tmp_path):
    mock_bot = AsyncMock()
    async def handler(config, run_date): return "/out.txt"

    task = _task(task_type="h")
    svc  = _svc(tasks=[task], handlers={"h": handler},
                status_file=tmp_path / "runs.json",
                telegram_token="tok", telegram_chat_id="123")
    svc._state = {}

    with patch("telegram.Bot", return_value=mock_bot):
        await svc._execute(task, "2026-06-17", TaskRunRecord(status="pending"), _now(7))

    mock_bot.send_message.assert_awaited_once()
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert "success" in call_kwargs.get("text", "").lower() or "✅" in call_kwargs.get("text", "")


@pytest.mark.asyncio
async def test_notify_skipped_when_no_token(tmp_path):
    mock_bot = AsyncMock()
    async def handler(config, run_date): return "/out.txt"

    task = _task(task_type="h")
    svc  = _svc(tasks=[task], handlers={"h": handler},
                status_file=tmp_path / "runs.json",
                telegram_token="", telegram_chat_id="123")
    svc._state = {}

    with patch("telegram.Bot", return_value=mock_bot):
        await svc._execute(task, "2026-06-17", TaskRunRecord(status="pending"), _now(7))

    mock_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_failure_sent_on_handler_exception(tmp_path):
    mock_bot = AsyncMock()
    async def handler(config, run_date): raise RuntimeError("boom")

    task = _task(task_type="h", max_retries=1)
    svc  = _svc(tasks=[task], handlers={"h": handler},
                status_file=tmp_path / "runs.json",
                telegram_token="tok", telegram_chat_id="123")
    svc._state = {}

    with patch("telegram.Bot", return_value=mock_bot):
        await svc._execute(task, "2026-06-17", TaskRunRecord(status="pending", attempts=0), _now(7))

    mock_bot.send_message.assert_awaited_once()
    text = mock_bot.send_message.call_args.kwargs.get("text", "")
    assert "failed" in text.lower() or "⚠️" in text
