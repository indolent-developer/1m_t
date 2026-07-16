"""
services.scheduler.run_status

Persist and query per-day task run state as JSON.

File layout (data/scheduler_runs.json):
{
  "2026-06-17": {
    "post_market_scan": {
      "status": "success",
      "attempts": 1,
      "last_run_at": "2026-06-17T02:05:00+02:00",
      "output_file": "data/daily_morning/post-market-movers/06.17.2026_post_movers.csv",
      "error": null
    },
    "prompt_1": {
      "status": "failed",
      "attempts": 2,
      "last_run_at": "2026-06-17T07:15:00+02:00",
      "output_file": null,
      "error": "yfinance timeout"
    }
  }
}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .models import TaskRunRecord


def _to_dict(record: TaskRunRecord) -> dict:
    return {
        "status":      record.status,
        "attempts":    record.attempts,
        "last_run_at": record.last_run_at,
        "output_file": record.output_file,
        "error":       record.error,
    }


def _from_dict(d: dict) -> TaskRunRecord:
    return TaskRunRecord(
        status      = d.get("status", "pending"),
        attempts    = d.get("attempts", 0),
        last_run_at = d.get("last_run_at"),
        output_file = d.get("output_file"),
        error       = d.get("error"),
    )


def load(path: Path) -> dict:
    """Load the full run-status dict from disk. Returns {} on missing/corrupt file."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(path: Path, data: dict) -> None:
    """Atomically write the run-status dict to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_record(data: dict, date_key: str, task_name: str) -> Optional[TaskRunRecord]:
    """Return the TaskRunRecord for (date, task), or None if not present."""
    raw = data.get(date_key, {}).get(task_name)
    return _from_dict(raw) if raw is not None else None


def set_record(data: dict, date_key: str, task_name: str, record: TaskRunRecord) -> None:
    """Upsert a TaskRunRecord into the in-memory state dict (does NOT write to disk)."""
    data.setdefault(date_key, {})[task_name] = _to_dict(record)


def prune_old_dates(data: dict, keep_days: int = 30) -> None:
    """Remove entries older than keep_days to prevent unbounded growth."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for key in [k for k in data if k < cutoff]:
        del data[key]
