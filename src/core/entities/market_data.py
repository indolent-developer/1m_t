"""
core.entities.market_data

News, market status, earnings time, and price quote entities.
Consolidates: StockNews, MarketNews, MarketStatus, EarningsTime, PriceQuote.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.entities.news_event import NewsEvent


class EarningsTime(Enum):
    """When an earnings report is released relative to the session."""
    BMO = "bmo"   # Before Market Open
    AMC = "amc"   # After Market Close


class MarketSession(Enum):
    PRE     = "pre"
    REGULAR = "regular"
    POST    = "post"
    CLOSED  = "closed"


@dataclass
class MarketStatus:
    exchange: str
    is_open:  bool
    session:  MarketSession
    timezone: str
    t:        int                  # Unix timestamp
    holiday:  Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "MarketStatus":
        return cls(
            exchange=data.get("exchange", ""),
            is_open=bool(data.get("isOpen", False)),
            session=MarketSession(data.get("session", "closed")),
            timezone=data.get("timezone", ""),
            t=int(data.get("t", 0)),
            holiday=data.get("holiday"),
        )


@dataclass
class StockNews:
    """News item tied to a specific ticker."""
    symbol:         str
    published_date: dt.datetime
    publisher:      str
    title:          str
    url:            str
    text:           str
    image:          str = ""
    site:           str = ""
    fetched_at:     Optional[dt.datetime] = None   # when our system received this article
    news_source:    str = ""                        # "FMP" | "Finnhub" | "Yahoo" | "IBKR"

    # Event-bus routing key — LocalEventBus.emit() dispatches on payload.event.
    # Must stay NEWS_PUBLISHED so NewsReactionAnalyzer and any other subscriber
    # actually receive this item when NewsMonitorService calls bus.emit(item).
    event: NewsEvent = NewsEvent.NEWS_PUBLISHED

    # Derived unique key — set by __post_init__
    key: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.key = f"{self.symbol}_{self.title}_{self.published_date.strftime('%Y%m%d%H%M%S')}"

    @property
    def latency_seconds(self) -> Optional[float]:
        if self.fetched_at and self.published_date:
            pub = self.published_date
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=dt.timezone.utc)
            return (self.fetched_at - pub).total_seconds()
        return None

    @classmethod
    def from_dict(cls, data: dict) -> "StockNews":
        import pytz as _pytz
        _ET = _pytz.timezone("America/New_York")
        date_raw = data.get("publishedDate", data.get("published_date", ""))
        if isinstance(date_raw, str):
            naive = dt.datetime.strptime(date_raw, "%Y-%m-%d %H:%M:%S")
            published = _ET.localize(naive)   # FMP publishedDate is US Eastern
        else:
            published = date_raw
        return cls(
            symbol=data.get("symbol", ""),
            published_date=published,
            publisher=data.get("publisher", ""),
            title=data.get("title", ""),
            url=data.get("url", ""),
            text=data.get("text", ""),
            image=data.get("image", ""),
            site=data.get("site", ""),
        )


@dataclass
class MarketNews:
    """Broad market / macro news item (not ticker-specific)."""
    category:       str
    time_published: int
    headline:       str
    id:             int
    source:         str
    summary:        str
    url:            str
    authors:        list[str]      = field(default_factory=list)
    text:           str            = ""
    tickers:        list[str]      = field(default_factory=list)
    sentiment_score: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict) -> "MarketNews":
        return cls(
            category=data.get("category", ""),
            time_published=int(data.get("time_published", 0)),
            headline=data.get("headline", ""),
            id=int(data.get("id", 0)),
            source=data.get("source", ""),
            summary=data.get("summary", ""),
            url=data.get("url", ""),
            authors=data.get("authors", []),
            text=data.get("text", ""),
            tickers=data.get("tickers", []),
            sentiment_score=data.get("overall_sentiment_score"),
        )


@dataclass
class PriceQuote:
    """Lightweight real-time price used for batch quote lookups (FMP batch endpoints)."""
    symbol:            str
    bid_price:         float = 0.0
    ask_price:         float = 0.0
    bid_size:          int   = 0
    ask_size:          int   = 0
    volume:            int   = 0
    timestamp:         int   = 0   # Unix ms
    change_percentage: float = 0.0
    day_high:          float = 0.0
    day_low:           float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2

    @property
    def price(self) -> float:
        return self.bid_price


@dataclass
class PriceTick:
    """
    Single price event emitted by the price monitor.

    `source` identifies who generated this tick — "finnhub" (WebSocket stream)
    or "fmp" (REST poll) — so subscribers know the data provenance and latency
    characteristics without inspecting anything else.
    """
    symbol:     str
    price:      float
    source:     str            # "finnhub" | "fmp"
    timestamp:  int   = 0     # Unix ms
    volume:     float = 0.0
    bid:        float = 0.0
    ask:        float = 0.0
    change_pct: float = 0.0
    day_high:   float = 0.0
    day_low:    float = 0.0
