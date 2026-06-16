"""
services.indicators_service

Thin wrappers around pandas-ta that add:
  - input validation  (required columns, min length, chronological order)
  - in-process caching  (same data + params → instant return)
  - normalised output column names

Expected DataFrame columns
--------------------------
  o   open
  h   high
  l   low
  c   close
  v   volume  (required only by vwap)
  t   UTC datetime — column or DatetimeIndex

All functions return a pd.Series (or pd.DataFrame for supertrend) with the
same index as the input.  Leading bars without enough history are NaN.

Usage
-----
    from services.indicators_service import atr, supertrend, ema, rsi, sma, vwap

    atr_s  = atr(df, length=14)
    st     = supertrend(df, length=10, multiplier=3.0)
    # st columns: value, direction (+1/-1), flipped (bool)
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta


# ── Cache ─────────────────────────────────────────────────────────────────────

_CACHE: dict[str, pd.Series | pd.DataFrame] = {}


def _fingerprint(df: pd.DataFrame, **params) -> str:
    if "t" in df.columns:
        t = df["t"]
        t0, t1 = str(t.iloc[0]), str(t.iloc[-1])
    elif isinstance(df.index, pd.DatetimeIndex):
        t0, t1 = str(df.index[0]), str(df.index[-1])
    else:
        t0, t1 = str(len(df)), "0"
    c    = df["c"]
    args = ":".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"{len(df)}|{t0}|{t1}|{float(c.iloc[0])}|{float(c.iloc[-1])}|{args}"


def cache_clear() -> None:
    """Flush the entire indicator cache (useful in tests)."""
    _CACHE.clear()


# ── Validation ────────────────────────────────────────────────────────────────

def _validate(df: pd.DataFrame, min_length: int, required: list[str]) -> None:
    if df is None or len(df) == 0:
        raise ValueError("DataFrame is empty")

    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    for col in required:
        n_nan = int(df[col].isna().sum())
        if n_nan:
            raise ValueError(f"Column '{col}' has {n_nan} NaN value(s)")

    if len(df) < min_length:
        raise ValueError(f"Need at least {min_length} bars, got {len(df)}")

    if isinstance(df.index, pd.DatetimeIndex):
        diffs = df.index.to_series().diff().iloc[1:]
        if (diffs <= pd.Timedelta(0)).any():
            bad = diffs[diffs <= pd.Timedelta(0)].index[0]
            raise ValueError(f"Timestamps not strictly ascending at index {bad}")
    elif "t" in df.columns and pd.api.types.is_datetime64_any_dtype(df["t"]):
        diffs = df["t"].diff().iloc[1:]
        if (diffs <= pd.Timedelta(0)).any():
            bad = diffs[diffs <= pd.Timedelta(0)].index[0]
            raise ValueError(f"Timestamps not strictly ascending at index {bad}")


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    Average True Range — pandas-ta ATR (Wilder smoothing).
    Returns pd.Series named 'atr', NaN for the first (length-1) bars.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    _validate(df, min_length=length + 1, required=["h", "l", "c"])

    key = _fingerprint(df, indicator="atr", length=length)
    if key in _CACHE:
        return _CACHE[key]

    result = ta.atr(df["h"], df["l"], df["c"], length=length).rename("atr")
    result.index = df.index
    _CACHE[key] = result
    return result


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """
    Exponential Moving Average of close.
    Returns pd.Series named 'ema', NaN for the first (length-1) bars.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    _validate(df, min_length=length, required=["c"])

    key = _fingerprint(df, indicator="ema", length=length)
    if key in _CACHE:
        return _CACHE[key]

    result = ta.ema(df["c"], length=length).rename("ema")
    result.index = df.index
    _CACHE[key] = result
    return result


# ── SMA ───────────────────────────────────────────────────────────────────────

def sma(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """
    Simple Moving Average of close.
    Returns pd.Series named 'sma', NaN for the first (length-1) bars.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    _validate(df, min_length=length, required=["c"])

    key = _fingerprint(df, indicator="sma", length=length)
    if key in _CACHE:
        return _CACHE[key]

    result = ta.sma(df["c"], length=length).rename("sma")
    result.index = df.index
    _CACHE[key] = result
    return result


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    Relative Strength Index — Wilder smoothing.
    Returns pd.Series named 'rsi', NaN for the first `length` bars.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    _validate(df, min_length=length + 1, required=["c"])

    key = _fingerprint(df, indicator="rsi", length=length)
    if key in _CACHE:
        return _CACHE[key]

    result = ta.rsi(df["c"], length=length).rename("rsi")
    result.index = df.index
    _CACHE[key] = result
    return result


# ── Supertrend ────────────────────────────────────────────────────────────────

def supertrend(
    df:         pd.DataFrame,
    length:     int   = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """
    Supertrend indicator — pandas-ta backend.

    Returns pd.DataFrame with columns:
      value      active support (long) or resistance (short) band
      direction  +1 = uptrend, -1 = downtrend  (NaN for warm-up bars)
      flipped    True on the bar where direction changed
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    if multiplier <= 0:
        raise ValueError(f"multiplier must be > 0, got {multiplier}")
    _validate(df, min_length=length + 1, required=["h", "l", "c"])

    key = _fingerprint(df, indicator="supertrend", length=length, multiplier=multiplier)
    if key in _CACHE:
        return _CACHE[key]

    raw = ta.supertrend(df["h"], df["l"], df["c"], length=length, multiplier=multiplier)
    # pandas-ta returns columns: SUPERT, SUPERTd, SUPERTl, SUPERTs
    supert_col  = f"SUPERT_{length}_{multiplier}"
    dir_col     = f"SUPERTd_{length}_{multiplier}"

    value     = raw[supert_col].rename("value")
    direction = raw[dir_col].rename("direction")      # already +1 / -1
    flipped   = direction.diff().ne(0) & direction.notna() & direction.shift(1).notna()
    flipped   = flipped.rename("flipped")

    result = pd.DataFrame(
        {"value": value, "direction": direction, "flipped": flipped},
        index=df.index,
    )
    _CACHE[key] = result
    return result


# ── ADX ──────────────────────────────────────────────────────────────────────

def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    Average Directional Index (strength only, not direction).
    Returns pd.Series named 'adx', NaN for warm-up bars.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    _validate(df, min_length=length * 2 + 1, required=["h", "l", "c"])

    key = _fingerprint(df, indicator="adx", length=length)
    if key in _CACHE:
        return _CACHE[key]

    raw = ta.adx(df["h"], df["l"], df["c"], length=length)
    col = f"ADX_{length}"
    result = raw[col].rename("adx")
    result.index = df.index
    _CACHE[key] = result
    return result


# ── VWAP ─────────────────────────────────────────────────────────────────────

def vwap(df: pd.DataFrame) -> pd.Series:
    """
    Volume-Weighted Average Price — cumulative, pandas-ta backend.
    Requires column 'v'.
    """
    _validate(df, min_length=1, required=["h", "l", "c", "v"])

    key = _fingerprint(df, indicator="vwap")
    if key in _CACHE:
        return _CACHE[key]

    result = ta.vwap(df["h"], df["l"], df["c"], df["v"]).rename("vwap")
    result.index = df.index
    _CACHE[key] = result
    return result
