from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from core.entities.analyst_data import PriceTargetConsensus
from core.entities.calendar_events import CalendarEventType
from core.entities.company_profile import CompanyProfile
from core.entities.earnings import EarningsCallTranscript
from core.entities.market_data import PriceQuote, StockNews
from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame


class DataFetcherBase(ABC):
    """Abstract base class for market data fetchers."""

    @abstractmethod
    def get_market_data(
        self,
        symbol: str,
        start: dt.datetime,
        end: dt.datetime,
        timeframe: TimeFrame,
        use_cache: bool = True,
    ) -> List[OHLCData]: ...

    @abstractmethod
    def get_price_batch(
        self,
        symbols: list[str],
        at: Optional[dt.datetime] = None,
    ) -> Dict[str, PriceQuote]: ...

    def get_last_price_batch(self, symbols: list[str]) -> Dict[str, PriceQuote]:
        return self.get_price_batch(symbols)

    def get_price(self, symbol: str, at: Optional[dt.datetime] = None) -> float:
        result = self.get_price_batch([symbol], at=at)
        quote = result.get(symbol)
        return quote.price if quote else 0.0

    def get_last_price(self, symbol: str) -> float:
        return self.get_price(symbol)

    @abstractmethod
    def get_calendar(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        event_types: List[CalendarEventType] | None = None,
    ) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_biggest_gainers(self) -> list: ...

    @abstractmethod
    def get_biggest_losers(self) -> list: ...

    @abstractmethod
    def get_company_overview(self, symbol: str) -> Optional[dict]: ...

    @abstractmethod
    def senate_trade(self, symbol: str) -> list: ...

    @abstractmethod
    def get_earnings_call_transcript(
        self,
        symbol: str,
        year: int | None = None,
        quarter: int | None = None,
    ) -> Optional[EarningsCallTranscript]: ...

    @abstractmethod
    def get_analyst_recommendations(self, symbol: str) -> list: ...

    @abstractmethod
    def get_upgrades_downgrades(self, symbol: str) -> list: ...

    @abstractmethod
    def get_news(self, symbol: str) -> List[StockNews]: ...

    @abstractmethod
    def get_company_profile(self, symbol: str) -> Optional[CompanyProfile]: ...

    @abstractmethod
    def get_price_target_consensus(self, symbol: str) -> Optional[PriceTargetConsensus]: ...

    @abstractmethod
    def get_stock_news(
        self,
        symbol: Optional[str] = None,
        page: int = 0,
        limit: int = 20,
    ) -> List[StockNews]: ...
