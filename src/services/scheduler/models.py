"""
services.scheduler.models

Data models for the generic scheduler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ScheduledTask:
    """
    Describes one recurring daily task.

    window_start / window_end:
        HH:MM strings in the given timezone.
        The task fires once per day, any time the scheduler's tick falls within
        this window and the task has not yet succeeded today.
        Windows must not cross midnight (window_end > window_start).

    task_type:
        Arbitrary string key — must match a handler registered with the scheduler.

    config:
        Passed verbatim to the handler. Put task-specific parameters here.

    max_retries:
        Maximum attempts per day (including the first). After this many failures
        the task is abandoned until the next calendar day.

    retry_delay_seconds:
        Minimum gap between consecutive attempts on the same day.
    """
    name: str
    task_type: str
    window_start: str               # "HH:MM"  e.g. "07:00"
    window_end: str                 # "HH:MM"  e.g. "09:00"
    timezone: str                   # e.g. "Europe/Berlin"
    config: dict[str, Any] = field(default_factory=dict)
    max_retries: int = 3
    retry_delay_seconds: int = 300  # 5 minutes between retries


@dataclass
class TaskRunRecord:
    """
    Persisted state for one (date, task) pair.

    status values:
        "pending"  — not yet attempted today
        "running"  — in-flight (set before execution; reverted to failed on crash)
        "success"  — completed successfully; will not re-run today
        "failed"   — last attempt failed; may retry if attempts < max_retries
    """
    status: str                        # "pending" | "running" | "success" | "failed"
    attempts: int = 0
    last_run_at: Optional[str] = None  # ISO 8601 with timezone
    output_file: Optional[str] = None  # path of the produced file on success
    error: Optional[str] = None        # last error message on failure
