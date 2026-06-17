"""
Tests for services.morning_enrichment.MorningEnrichmentService

yfinance calls are mocked — no network access required.
Filesystem tests use tmp_path.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pandas as pd
import pytest
from services.morning_enrichment import MorningEnrichmentService


# ── helpers ───────────────────────────────────────────────────────────────────

_TODAY = dt.date(2026, 6, 17)

def _svc(tmp_path: Path) -> MorningEnrichmentService:
    return MorningEnrichmentService(data_root=tmp_path)


def _movers_dir(tmp_path: Path) -> Path:
    d = tmp_path / "daily_morning" / "post-market-movers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_csv(directory: Path, filename: str, symbols=("TENB", "LION")) -> Path:
    rows = [
        {
            "name": sym, "description": f"{sym} Corp", "sector": "Technology",
            "postmarket_change": 5.0, "postmarket_volume": 400_000,
            "postmarket_close": 28.0, "close": 26.0, "change": -2.0,
            "volume": 1_500_000, "average_volume_10d_calc": 1_200_000,
            "average_volume_30d_calc": 1_400_000,
            "relative_volume_intraday|5": 1.1,
            "market_cap_basic": 3_000_000_000,
            "RSI": 42.0, "ADX": 18.0, "ADX+DI": 16.0, "ADX-DI": 21.0,
            "EMA20": 28.0, "EMA50": 30.0,
        }
        for sym in symbols
    ]
    path = directory / filename
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _mock_yf_ticker(bars: list[dict] | None = None):
    """Return a mock yfinance Ticker whose history() returns a DataFrame."""
    if bars is None:
        bars = [
            {"Open": 29.0, "High": 30.0, "Low": 28.0, "Close": 29.5, "Volume": 1_000_000},
            {"Open": 28.5, "High": 29.5, "Low": 27.5, "Close": 28.8, "Volume": 1_100_000},
            {"Open": 28.0, "High": 29.0, "Low": 27.0, "Close": 28.0, "Volume": 1_200_000},
            {"Open": 27.5, "High": 28.5, "Low": 26.5, "Close": 27.5, "Volume": 1_300_000},
            {"Open": 27.0, "High": 28.0, "Low": 26.0, "Close": 26.9, "Volume": 1_400_000},
        ]
    dates = pd.date_range("2026-06-11", periods=len(bars), freq="B")
    df = pd.DataFrame(bars, index=dates)
    ticker = MagicMock()
    ticker.history.return_value = df
    return ticker


# ── _find_movers_csv ──────────────────────────────────────────────────────────

def test_find_movers_csv_returns_correct_path_when_exists(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "17.06.2026_post_movers.csv")
    svc  = _svc(tmp_path)
    path = svc._find_movers_csv(_TODAY)
    assert path is not None
    assert path.name == "17.06.2026_post_movers.csv"


def test_find_movers_csv_returns_none_when_empty_directory(tmp_path):
    _movers_dir(tmp_path)
    svc = _svc(tmp_path)
    assert svc._find_movers_csv(_TODAY) is None


def test_find_movers_csv_falls_back_to_most_recent(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "16.06.2026_post_movers.csv")  # yesterday's file
    svc  = _svc(tmp_path)
    path = svc._find_movers_csv(_TODAY)           # today's file missing
    assert path is not None
    assert path.name == "16.06.2026_post_movers.csv"


def test_find_movers_csv_prefers_today_over_fallback(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "16.06.2026_post_movers.csv")
    _write_csv(d, "17.06.2026_post_movers.csv")
    svc  = _svc(tmp_path)
    path = svc._find_movers_csv(_TODAY)
    assert path.name == "17.06.2026_post_movers.csv"


# ── _fetch_5day_ohlc ──────────────────────────────────────────────────────────

def test_fetch_5day_ohlc_returns_5_bars_per_symbol(tmp_path):
    svc = _svc(tmp_path)
    df  = pd.DataFrame([{"name": "TENB", "close": 26.9}])

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        result = svc._fetch_5day_ohlc(["TENB"], df)

    assert "TENB" in result
    assert len(result["TENB"]) == 5


def test_fetch_5day_ohlc_marks_last_bar_as_today(tmp_path):
    svc = _svc(tmp_path)
    df  = pd.DataFrame([{"name": "TENB", "close": 26.9}])

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        result = svc._fetch_5day_ohlc(["TENB"], df)

    bars = result["TENB"]
    assert bars[-1]["today"] is True
    assert all(not b["today"] for b in bars[:-1])


def test_fetch_5day_ohlc_prefers_tv_close_for_today(tmp_path):
    svc = _svc(tmp_path)
    # TV close is 27.5; yfinance close on last bar is 26.9
    df  = pd.DataFrame([{"name": "TENB", "close": 27.5}])

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        result = svc._fetch_5day_ohlc(["TENB"], df)

    assert result["TENB"][-1]["close"] == pytest.approx(27.5)


def test_fetch_5day_ohlc_returns_empty_list_on_yfinance_error(tmp_path):
    svc = _svc(tmp_path)
    df  = pd.DataFrame([{"name": "TENB", "close": 26.9}])

    error_ticker = MagicMock()
    error_ticker.history.side_effect = RuntimeError("network error")

    with patch("yfinance.Ticker", return_value=error_ticker):
        result = svc._fetch_5day_ohlc(["TENB"], df)

    assert result["TENB"] == []


def test_fetch_5day_ohlc_returns_empty_list_on_empty_history(tmp_path):
    svc = _svc(tmp_path)
    df  = pd.DataFrame([{"name": "TENB", "close": 26.9}])

    empty_ticker = MagicMock()
    empty_ticker.history.return_value = pd.DataFrame()

    with patch("yfinance.Ticker", return_value=empty_ticker):
        result = svc._fetch_5day_ohlc(["TENB"], df)

    assert result["TENB"] == []


def test_fetch_5day_ohlc_handles_multiple_symbols(tmp_path):
    svc = _svc(tmp_path)
    df  = pd.DataFrame([
        {"name": "TENB", "close": 26.9},
        {"name": "LION", "close": 15.5},
    ])

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        result = svc._fetch_5day_ohlc(["TENB", "LION"], df)

    assert "TENB" in result and "LION" in result


def test_fetch_5day_ohlc_caps_at_5_bars_even_with_more_history(tmp_path):
    svc = _svc(tmp_path)
    df  = pd.DataFrame([{"name": "TENB", "close": 26.9}])

    # Return 8 bars; should be trimmed to 5
    many_bars = [
        {"Open": 30.0 - i, "High": 31.0 - i, "Low": 29.0 - i,
         "Close": 30.5 - i, "Volume": 1_000_000}
        for i in range(8)
    ]
    with patch("yfinance.Ticker", return_value=_mock_yf_ticker(many_bars)):
        result = svc._fetch_5day_ohlc(["TENB"], df)

    assert len(result["TENB"]) == 5


# ── build_and_save_prompt ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_and_save_prompt_raises_for_missing_csv(tmp_path):
    _movers_dir(tmp_path)  # create dir but no CSV
    svc = _svc(tmp_path)
    with pytest.raises(FileNotFoundError):
        await svc.build_and_save_prompt(1, _TODAY)


@pytest.mark.asyncio
async def test_build_and_save_prompt_raises_for_invalid_prompt_num(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "17.06.2026_post_movers.csv")
    svc = _svc(tmp_path)
    with pytest.raises(ValueError):
        await svc.build_and_save_prompt(0, _TODAY)


@pytest.mark.asyncio
async def test_build_and_save_prompt_creates_output_file(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "17.06.2026_post_movers.csv")
    svc = _svc(tmp_path)

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        out = await svc.build_and_save_prompt(1, _TODAY)

    assert Path(out).exists()


@pytest.mark.asyncio
async def test_build_and_save_prompt_output_path_matches_convention(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "17.06.2026_post_movers.csv")
    svc = _svc(tmp_path)

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        out = await svc.build_and_save_prompt(1, _TODAY)

    assert Path(out).name == "17.06.2026_prompt1.txt"


@pytest.mark.asyncio
async def test_build_and_save_prompt_output_contains_symbols(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "17.06.2026_post_movers.csv", symbols=("TENB", "LION"))
    svc = _svc(tmp_path)

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        out = await svc.build_and_save_prompt(1, _TODAY)

    content = Path(out).read_text()
    assert "TENB" in content
    assert "LION" in content


@pytest.mark.asyncio
async def test_build_and_save_prompt_creates_all_three_prompts(tmp_path):
    d = _movers_dir(tmp_path)
    _write_csv(d, "17.06.2026_post_movers.csv")
    svc = _svc(tmp_path)

    with patch("yfinance.Ticker", return_value=_mock_yf_ticker()):
        for n in (1, 2, 3):
            await svc.build_and_save_prompt(n, _TODAY)

    prompts_dir = tmp_path / "daily_morning" / "prompts"
    assert (prompts_dir / "17.06.2026_prompt1.txt").exists()
    assert (prompts_dir / "17.06.2026_prompt2.txt").exists()
    assert (prompts_dir / "17.06.2026_prompt3.txt").exists()
