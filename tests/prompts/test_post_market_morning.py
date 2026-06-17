"""
Tests for prompts.post_market_morning

No external calls — everything is constructed from plain dicts / DataFrames.
"""
from __future__ import annotations

import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import pytest
import pandas as pd
from prompts.post_market_morning import (
    PROMPT_1,
    PROMPT_2,
    PROMPT_3,
    build_prompt,
    format_ticker_block,
    format_ticker_data,
)

# ── fixtures ──────────────────────────────────────────────────────────────────

def _row(
    symbol="TENB",
    desc="Tenable Holdings, Inc.",
    sector="Technology",
    pm_chg=5.87,
    pm_vol=424548.0,
    pm_close=28.5,
    close=26.92,
    day_chg=-2.89,
    volume=1971943.0,
    avg10=1500000.0,
    avg30=1800000.0,
    relvol=1.1,
    mktcap=3_400_000_000.0,
    rsi=42.1,
    adx=18.3,
    adx_p=16.2,
    adx_m=21.4,
    ema20=28.4,
    ema50=30.1,
) -> pd.Series:
    return pd.Series({
        "name": symbol,
        "description": desc,
        "sector": sector,
        "postmarket_change": pm_chg,
        "postmarket_volume": pm_vol,
        "postmarket_close": pm_close,
        "close": close,
        "change": day_chg,
        "volume": volume,
        "average_volume_10d_calc": avg10,
        "average_volume_30d_calc": avg30,
        "relative_volume_intraday|5": relvol,
        "market_cap_basic": mktcap,
        "RSI": rsi,
        "ADX": adx,
        "ADX+DI": adx_p,
        "ADX-DI": adx_m,
        "EMA20": ema20,
        "EMA50": ema50,
    })


def _ohlc() -> list[dict]:
    return [
        {"date": "Jun 11 Wed", "open": 29.12, "high": 29.80, "low": 28.44, "close": 28.65, "volume": 1_200_000, "today": False},
        {"date": "Jun 12 Thu", "open": 28.80, "high": 29.20, "low": 27.90, "close": 28.30, "volume": 1_400_000, "today": False},
        {"date": "Jun 13 Fri", "open": 28.10, "high": 28.70, "low": 27.40, "close": 27.65, "volume": 1_600_000, "today": False},
        {"date": "Jun 16 Mon", "open": 27.50, "high": 28.00, "low": 26.50, "close": 27.65, "volume": 2_000_000, "today": False},
        {"date": "Jun 17 Tue", "open": 26.90, "high": 27.50, "low": 26.20, "close": 26.92, "volume": 1_971_943, "today": True},
    ]


# ── format_ticker_block — content ─────────────────────────────────────────────

def test_block_contains_symbol():
    block = format_ticker_block(_row())
    assert "TENB" in block


def test_block_contains_description():
    block = format_ticker_block(_row())
    assert "Tenable Holdings" in block


def test_block_contains_sector():
    block = format_ticker_block(_row())
    assert "Technology" in block


def test_block_contains_pm_change():
    block = format_ticker_block(_row(pm_chg=5.87))
    assert "5.87" in block


def test_block_contains_pm_volume():
    block = format_ticker_block(_row(pm_vol=424548))
    # formatted as "425K"
    assert "425K" in block or "424K" in block


def test_block_contains_close_price():
    block = format_ticker_block(_row(close=26.92))
    assert "26.92" in block


def test_block_contains_rsi():
    block = format_ticker_block(_row(rsi=42.1))
    assert "42.10" in block


def test_block_contains_adx():
    block = format_ticker_block(_row(adx=18.3))
    assert "18.30" in block


def test_block_contains_ema20():
    block = format_ticker_block(_row(ema20=28.4))
    assert "28.40" in block


def test_block_contains_market_cap():
    block = format_ticker_block(_row(mktcap=3_400_000_000))
    assert "3.40B" in block


def test_block_upward_arrow_for_positive_pm():
    block = format_ticker_block(_row(pm_chg=5.87))
    assert "▲" in block


def test_block_downward_arrow_for_negative_pm():
    block = format_ticker_block(_row(pm_chg=-5.14))
    assert "▼" in block


# ── format_ticker_block — NaN/None handling ───────────────────────────────────

def test_block_renders_dash_for_none_rsi():
    row = _row()
    row["RSI"] = None
    block = format_ticker_block(row)
    assert "RSI: —" in block


def test_block_renders_dash_for_nan_adx():
    row = _row()
    row["ADX"] = float("nan")
    block = format_ticker_block(row)
    assert "ADX: —" in block


def test_block_renders_dash_for_nan_mktcap():
    row = _row()
    row["market_cap_basic"] = float("nan")
    block = format_ticker_block(row)
    assert "MCap: —" in block


def test_block_renders_dash_for_none_volume():
    row = _row()
    row["volume"] = None
    block = format_ticker_block(row)
    # Should not raise; should have a dash in volume position
    assert "—" in block


# ── format_ticker_block — OHLC section ───────────────────────────────────────

def test_block_includes_ohlc_section_when_provided():
    block = format_ticker_block(_row(), _ohlc())
    assert "5-DAY OHLC" in block


def test_block_ohlc_contains_all_five_dates():
    block = format_ticker_block(_row(), _ohlc())
    for date_label in ["Jun 11", "Jun 12", "Jun 13", "Jun 16", "Jun 17"]:
        assert date_label in block


def test_block_ohlc_today_marker():
    block = format_ticker_block(_row(), _ohlc())
    assert "← today" in block


def test_block_no_ohlc_section_when_none():
    block = format_ticker_block(_row(), None)
    assert "5-DAY OHLC" not in block


def test_block_no_ohlc_section_when_empty_list():
    block = format_ticker_block(_row(), [])
    assert "5-DAY OHLC" not in block


# ── format_ticker_data — DataFrame input ─────────────────────────────────────

def test_format_ticker_data_accepts_dataframe():
    df = pd.DataFrame([_row().to_dict(), _row(symbol="LION", desc="Lionsgate").to_dict()])
    text = format_ticker_data(df)
    assert "TENB" in text
    assert "LION" in text


def test_format_ticker_data_uses_ohlc_map():
    df    = pd.DataFrame([_row().to_dict()])
    ohlc  = {"TENB": _ohlc()}
    text  = format_ticker_data(df, ohlc)
    assert "5-DAY OHLC" in text


def test_format_ticker_data_separates_tickers():
    df   = pd.DataFrame([_row().to_dict(), _row(symbol="LION", desc="Lionsgate").to_dict()])
    text = format_ticker_data(df)
    # Two header separators expected (one per ticker block)
    assert text.count("━" * 10) >= 2


# ── build_prompt ─────────────────────────────────────────────────────────────

def test_build_prompt_raises_for_invalid_num():
    with pytest.raises(ValueError):
        build_prompt(0, "data")
    with pytest.raises(ValueError):
        build_prompt(4, "data")


def test_build_prompt_1_contains_role_section():
    text = build_prompt(1, "ticker_data_here")
    assert "ROLE" in text
    assert "1:00 am ET" in text


def test_build_prompt_2_contains_role_section():
    text = build_prompt(2, "ticker_data_here")
    assert "ROLE" in text
    assert "8:00 am ET" in text


def test_build_prompt_3_contains_role_section():
    text = build_prompt(3, "ticker_data_here")
    assert "ROLE" in text
    assert "10:05 am ET" in text


def test_build_prompt_injects_ticker_data():
    text = build_prompt(1, "TENB_BLOCK_HERE")
    assert "TENB_BLOCK_HERE" in text


def test_build_prompt_injects_ticker_list():
    text = build_prompt(1, "data", ticker_list="TENB, LION, AAPL")
    assert "TENB, LION, AAPL" in text


def test_build_prompt_2_has_prev_output_placeholder():
    text = build_prompt(2, "data")
    assert "[PASTE PREVIOUS RUN OUTPUT HERE]" in text


def test_build_prompt_3_has_prev_output_placeholder():
    text = build_prompt(3, "data")
    assert "[PASTE PREVIOUS RUN OUTPUT HERE]" in text


def test_build_prompt_2_fills_prev_output_when_provided():
    text = build_prompt(2, "data", prev_run_output="Run 1 was here")
    assert "Run 1 was here" in text
    assert "[PASTE PREVIOUS RUN OUTPUT HERE]" not in text


def test_build_prompt_1_no_prev_run_section():
    # Prompt 1 has no {prev_run_output} placeholder
    text = build_prompt(1, "data")
    assert "[PASTE PREVIOUS RUN OUTPUT HERE]" not in text


def test_build_prompt_contains_framework_section():
    for n in (1, 2, 3):
        assert "FRAMEWORK" in build_prompt(n, "d")


def test_build_prompt_contains_rules_section():
    for n in (1, 2, 3):
        assert "RULES" in build_prompt(n, "d")
