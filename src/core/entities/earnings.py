"""
core.entities.earnings

All earnings-related entities in one module.
Consolidates: EarningsCalendar, EarningsReport, EarningsCallTranscript.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

DECIMALS = 4  # Replace with import from core.constants if available


@dataclass
class EarningsCalendar:
    """Upcoming or past earnings event for a ticker."""
    symbol:           str
    date:             Optional[dt.date]
    year:             Optional[int]
    quarter:          Optional[int]
    hour:             Optional[str]          # "bmo" | "amc"
    eps_actual:       Optional[float] = None
    eps_estimate:     Optional[float] = None
    revenue_actual:   Optional[int]   = None
    revenue_estimate: Optional[int]   = None

    @classmethod
    def from_dict(cls, data: dict) -> "EarningsCalendar":
        def _f(v) -> Optional[float]:
            try: return float(v)
            except (TypeError, ValueError): return None
        def _i(v) -> Optional[int]:
            try: return int(v)
            except (TypeError, ValueError): return None

        date_raw = data.get("date")
        return cls(
            symbol=data.get("symbol", ""),
            date=dt.datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else None,
            year=_i(data.get("year")),
            quarter=_i(data.get("quarter")),
            hour=data.get("hour"),
            eps_actual=_f(data.get("epsActual")),
            eps_estimate=_f(data.get("epsEstimate")),
            revenue_actual=_i(data.get("revenueActual")),
            revenue_estimate=_i(data.get("revenueEstimate")),
        )


@dataclass
class EarningsReport:
    """Reported financials for a single quarter/period."""
    symbol:                      str
    filing_date:                 Optional[dt.datetime]
    period_end_date:             Optional[dt.datetime]
    total_revenue:               Optional[float]
    net_income:                  Optional[float]
    gross_profit:                Optional[float]
    earnings_per_share_basic:    Optional[float] = None
    earnings_per_share_diluted:  Optional[float] = None
    current_assets:              Optional[float] = None
    total_assets:                Optional[float] = None
    current_liabilities:         Optional[float] = None
    total_liabilities:           Optional[float] = None
    total_debt:                  Optional[float] = None
    inventory:                   Optional[float] = None
    stockholders_equity:         Optional[float] = None

    def __post_init__(self) -> None:
        cl  = self.current_liabilities
        ta  = self.total_assets
        se  = self.stockholders_equity
        inv = self.inventory
        td  = self.total_debt
        ni  = self.net_income
        ca  = self.current_assets
        eps = self.earnings_per_share_diluted
        rev = self.total_revenue

        self.current_ratio   = round(ca / cl, DECIMALS) if ca and cl else None
        self.quick_ratio     = round((ca - inv) / cl, DECIMALS) if ca and inv and cl else None
        self.debt_ratio      = round(self.total_liabilities / ta, DECIMALS) if self.total_liabilities and ta else None
        self.debt_to_equity  = round(td / se, DECIMALS) if td and se else None
        self.return_on_assets = round(ni / ta, DECIMALS) if ni and ta else None
        self.return_on_equity = round(ni / se, DECIMALS) if ni and se else None
        self.earnings_yield  = round(eps / rev, DECIMALS) if eps and rev else None

    @staticmethod
    def from_dict(data: dict) -> "EarningsReport":
        def _f(v) -> Optional[float]:
            try: return float(v)
            except (TypeError, ValueError): return None
        def _dt(v) -> Optional[dt.datetime]:
            try: return dt.datetime.strptime(v, "%Y-%m-%d") if v else None
            except ValueError: return None

        return EarningsReport(
            symbol=data.get("symbol", ""),
            filing_date=_dt(data.get("filing_date")),
            period_end_date=_dt(data.get("period_end_date")),
            total_revenue=_f(data.get("total_revenue")),
            net_income=_f(data.get("net_income")),
            gross_profit=_f(data.get("gross_profit")),
            earnings_per_share_basic=_f(data.get("earnings_per_share_basic")),
            earnings_per_share_diluted=_f(data.get("earnings_per_share_diluted")),
            current_assets=_f(data.get("current_assets")),
            total_assets=_f(data.get("total_assets")),
            current_liabilities=_f(data.get("current_liabilities")),
            total_liabilities=_f(data.get("total_liabilities")),
            total_debt=_f(data.get("total_debt")),
            inventory=_f(data.get("inventory")),
            stockholders_equity=_f(data.get("stockholders_equity")),
        )


@dataclass
class EarningsCallTranscript:
    symbol:     str
    date:       dt.datetime
    year:       int
    quarter:    int
    transcript: str

    @classmethod
    def from_dict(cls, data: dict) -> "EarningsCallTranscript":
        date_raw = data.get("date", "")
        return cls(
            symbol=data.get("symbol", ""),
            date=dt.datetime.strptime(date_raw, "%Y-%m-%d") if date_raw else None,
            year=int(data.get("year", 0)),
            quarter=int(data.get("quarter", 0)),
            transcript=data.get("transcript", ""),
        )
