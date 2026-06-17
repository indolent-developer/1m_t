"""
Tests for services.scheduler.run_status

All tests use tmp_path — no real filesystem side effects.
"""
from __future__ import annotations

import json
import sys
import os
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../src"))

import pytest
from services.scheduler.models import TaskRunRecord
from services.scheduler import run_status as rs


# ── helpers ───────────────────────────────────────────────────────────────────

def _record(
    status="success",
    attempts=1,
    last_run_at="2026-06-17T07:01:00+02:00",
    output_file="/data/prompts/17.06.2026_prompt1.txt",
    error=None,
) -> TaskRunRecord:
    return TaskRunRecord(
        status=status,
        attempts=attempts,
        last_run_at=last_run_at,
        output_file=output_file,
        error=error,
    )


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ── load ──────────────────────────────────────────────────────────────────────

def test_load_returns_empty_when_file_missing(tmp_path):
    result = rs.load(tmp_path / "nope.json")
    assert result == {}


def test_load_returns_empty_on_corrupt_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("NOT VALID JSON {{{{")
    assert rs.load(p) == {}


def test_load_returns_data_from_valid_file(tmp_path):
    p = tmp_path / "runs.json"
    _write_json(p, {"2026-06-17": {"task_a": {"status": "success", "attempts": 1,
                                               "last_run_at": None, "output_file": None, "error": None}}})
    data = rs.load(p)
    assert "2026-06-17" in data
    assert data["2026-06-17"]["task_a"]["status"] == "success"


def test_load_returns_empty_dict_not_list_on_empty_file(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("")
    assert rs.load(p) == {}


# ── save ──────────────────────────────────────────────────────────────────────

def test_save_writes_valid_json(tmp_path):
    p = tmp_path / "runs.json"
    rs.save(p, {"2026-06-17": {"t": {"status": "pending"}}})
    loaded = json.loads(p.read_text())
    assert loaded["2026-06-17"]["t"]["status"] == "pending"


def test_save_creates_parent_directories(tmp_path):
    p = tmp_path / "deep" / "nested" / "runs.json"
    rs.save(p, {})
    assert p.exists()


def test_save_no_leftover_tmp_file(tmp_path):
    p = tmp_path / "runs.json"
    rs.save(p, {"k": "v"})
    tmp = p.with_suffix(".tmp")
    assert not tmp.exists()


def test_save_roundtrip(tmp_path):
    p = tmp_path / "runs.json"
    original = {"2026-06-17": {"scan": {"status": "failed", "attempts": 2,
                                        "last_run_at": "2026-06-17T02:05:00+02:00",
                                        "output_file": None, "error": "timeout"}}}
    rs.save(p, original)
    assert rs.load(p) == original


# ── get_record ────────────────────────────────────────────────────────────────

def test_get_record_returns_none_when_date_missing():
    assert rs.get_record({}, "2026-06-17", "scan") is None


def test_get_record_returns_none_when_task_missing():
    data = {"2026-06-17": {}}
    assert rs.get_record(data, "2026-06-17", "scan") is None


def test_get_record_returns_record_with_correct_status():
    data = {"2026-06-17": {"scan": {"status": "success", "attempts": 1,
                                    "last_run_at": None, "output_file": "/f", "error": None}}}
    rec = rs.get_record(data, "2026-06-17", "scan")
    assert rec is not None
    assert rec.status == "success"
    assert rec.attempts == 1
    assert rec.output_file == "/f"


def test_get_record_handles_missing_optional_fields():
    data = {"2026-06-17": {"scan": {"status": "pending"}}}
    rec = rs.get_record(data, "2026-06-17", "scan")
    assert rec is not None
    assert rec.attempts == 0
    assert rec.last_run_at is None
    assert rec.error is None


def test_get_record_preserves_error_message():
    data = {"2026-06-17": {"p1": {"status": "failed", "attempts": 2,
                                   "last_run_at": None, "output_file": None,
                                   "error": "connection refused"}}}
    rec = rs.get_record(data, "2026-06-17", "p1")
    assert rec.error == "connection refused"


# ── set_record ────────────────────────────────────────────────────────────────

def test_set_record_creates_date_key_when_missing():
    data = {}
    rs.set_record(data, "2026-06-17", "scan", _record())
    assert "2026-06-17" in data


def test_set_record_stores_all_fields():
    data = {}
    rs.set_record(data, "2026-06-17", "prompt_1", _record(
        status="failed", attempts=3, error="timeout"
    ))
    stored = data["2026-06-17"]["prompt_1"]
    assert stored["status"]   == "failed"
    assert stored["attempts"] == 3
    assert stored["error"]    == "timeout"


def test_set_record_overwrites_existing():
    data = {"2026-06-17": {"scan": {"status": "running", "attempts": 1,
                                    "last_run_at": None, "output_file": None, "error": None}}}
    rs.set_record(data, "2026-06-17", "scan", _record(status="success"))
    assert data["2026-06-17"]["scan"]["status"] == "success"


def test_set_record_multiple_tasks_same_date():
    data = {}
    rs.set_record(data, "2026-06-17", "scan",     _record(status="success"))
    rs.set_record(data, "2026-06-17", "prompt_1", _record(status="pending"))
    assert data["2026-06-17"]["scan"]["status"]     == "success"
    assert data["2026-06-17"]["prompt_1"]["status"] == "pending"


# ── prune_old_dates ───────────────────────────────────────────────────────────

def test_prune_removes_old_dates():
    old = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    data = {old: {"scan": {}}, "2099-01-01": {"scan": {}}}
    rs.prune_old_dates(data, keep_days=30)
    assert old not in data
    assert "2099-01-01" in data


def test_prune_keeps_recent_dates():
    today = date.today().strftime("%Y-%m-%d")
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    data = {today: {}, yesterday: {}}
    rs.prune_old_dates(data, keep_days=30)
    assert today in data
    assert yesterday in data


def test_prune_empty_data_is_no_op():
    data = {}
    rs.prune_old_dates(data)
    assert data == {}
