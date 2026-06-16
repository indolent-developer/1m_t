"""
core.entities.analyst_data

Analyst ratings, price targets, and grading entities.
Consolidates: Grade, GradesSummary, GradeType,
              AnalystRatingSnapshot, PriceTargetConsensus, PriceTargetNews.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ── Grade enums & models ──────────────────────────────────────────────────────

class GradeType(Enum):
    ACCUMULATE         = "Accumulate"
    LONG_TERM_BUY      = "Long Term Buy"
    OUTPERFORM         = "Outperform"
    SECTOR_OUTPERFORM  = "Sector Outperform"
    PERFORM            = "Perform"
    PEER_PERFORM       = "Peer Perform"
    STRONG_BUY         = "Strong Buy"
    OVERWEIGHT         = "Overweight"
    SECTOR_WEIGHT      = "Sector Weight"
    STRONG_SELL        = "Strong Sell"
    BUY                = "Buy"
    MARKET_PERFORM     = "Market Perform"
    MARKET_OUTPERFORM  = "Market Outperform"
    HOLD               = "Hold"
    SECTOR_PERFORM     = "Sector Perform"
    UNDERPERFORM       = "Underperform"
    POSITIVE           = "Positive"
    NEGATIVE           = "Negative"
    REDUCE             = "Reduce"
    NEUTRAL            = "Neutral"
    EQUAL_WEIGHT       = "Equal Weight"
    IN_LINE            = "In Line"
    UNDERWEIGHT        = "Underweight"
    SELL               = "Sell"


@dataclass
class Grade:
    symbol:          str
    date:            dt.date
    grading_company: str
    action:          str
    previous_grade:  Optional[GradeType] = None
    new_grade:       Optional[GradeType] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Grade":
        date_raw = data.get("date", "")
        parsed_date = (
            dt.datetime.strptime(date_raw, "%Y-%m-%d").date()
            if isinstance(date_raw, str) and date_raw else None
        )
        prev = data.get("previousGrade")
        new  = data.get("newGrade")
        return cls(
            symbol=data.get("symbol", ""),
            date=parsed_date,
            grading_company=data.get("gradingCompany", ""),
            action=data.get("action", ""),
            previous_grade=GradeType(prev) if prev else None,
            new_grade=GradeType(new) if new else None,
        )


@dataclass
class GradesSummary:
    symbol:      str
    strong_buy:  int
    buy:         int
    hold:        int
    sell:        int
    strong_sell: int
    consensus:   str


# ── Analyst scoring ───────────────────────────────────────────────────────────

@dataclass
class AnalystRatingSnapshot:
    """DCF + ratio-based analyst scoring snapshot."""
    symbol:                       str
    rating:                       str
    overall_score:                int
    discounted_cash_flow_score:   int
    return_on_equity_score:       int
    return_on_assets_score:       int
    debt_to_equity_score:         int
    price_to_earnings_score:      int
    price_to_book_score:          int

    @classmethod
    def from_dict(cls, data: dict) -> "AnalystRatingSnapshot":
        return cls(
            symbol=data.get("symbol", ""),
            rating=data.get("rating", "N/A"),
            overall_score=data.get("overallScore", 0),
            discounted_cash_flow_score=data.get("discountedCashFlowScore", 0),
            return_on_equity_score=data.get("returnOnEquityScore", 0),
            return_on_assets_score=data.get("returnOnAssetsScore", 0),
            debt_to_equity_score=data.get("debtToEquityScore", 0),
            price_to_earnings_score=data.get("priceToEarningsScore", 0),
            price_to_book_score=data.get("priceToBookScore", 0),
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "rating": self.rating,
            "overall_score": self.overall_score,
            "discounted_cash_flow_score": self.discounted_cash_flow_score,
            "return_on_equity_score": self.return_on_equity_score,
            "return_on_assets_score": self.return_on_assets_score,
            "debt_to_equity_score": self.debt_to_equity_score,
            "price_to_earnings_score": self.price_to_earnings_score,
            "price_to_book_score": self.price_to_book_score,
        }


# ── Price targets ─────────────────────────────────────────────────────────────

@dataclass
class PriceTargetConsensus:
    symbol:           str
    target_high:      Optional[float] = None
    target_low:       Optional[float] = None
    target_consensus: Optional[float] = None
    target_median:    Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict) -> "PriceTargetConsensus":
        def _f(v) -> Optional[float]:
            try: return float(v)
            except (TypeError, ValueError): return None

        return cls(
            symbol=data.get("symbol", ""),
            target_high=_f(data.get("targetHigh")),
            target_low=_f(data.get("targetLow")),
            target_consensus=_f(data.get("targetConsensus")),
            target_median=_f(data.get("targetMedian")),
        )

    @property
    def has_valid_data(self) -> bool:
        return any(v is not None for v in (
            self.target_high, self.target_low,
            self.target_consensus, self.target_median,
        ))

    @property
    def price_range(self) -> Optional[float]:
        if self.target_high is not None and self.target_low is not None:
            return self.target_high - self.target_low
        return None


@dataclass
class PriceTargetNews:
    symbol:             str
    published_date:     Optional[dt.datetime]
    news_url:           str
    news_title:         str
    analyst_name:       str
    analyst_company:    str
    news_publisher:     str
    news_base_url:      str
    price_target:       Optional[float] = None
    adj_price_target:   Optional[float] = None
    price_when_posted:  Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict) -> "PriceTargetNews":
        date_raw = data.get("publishedDate")
        parsed = None
        if date_raw:
            try:
                parsed = dt.datetime.strptime(date_raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=dt.timezone.utc
                )
            except ValueError:
                pass

        def _f(v) -> Optional[float]:
            try: return float(v)
            except (TypeError, ValueError): return None

        return cls(
            symbol=data.get("symbol", ""),
            published_date=parsed,
            news_url=data.get("newsURL", ""),
            news_title=data.get("newsTitle", ""),
            analyst_name=data.get("analystName", ""),
            analyst_company=data.get("analystCompany", ""),
            news_publisher=data.get("newsPublisher", ""),
            news_base_url=data.get("newsBaseURL", ""),
            price_target=_f(data.get("priceTarget")),
            adj_price_target=_f(data.get("adjPriceTarget")),
            price_when_posted=_f(data.get("priceWhenPosted")),
        )
