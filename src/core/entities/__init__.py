"""
core.entities

Public re-exports — import everything from here.
"""
# Base
from core.entities.base_entity import BaseEntity, parse_datetime, parse_date, to_float, to_int

# Instrument classification
from core.entities.instrument_type import InstrumentType
from core.entities.time_frame import TimeFrame
from core.entities.calendar_events import CalendarEventType

# Broker / trading primitives
from core.entities.broker_entities import (
    DataSource, OrderSide, TradeSide, OrderType, Action,
    OrderStatus, TradeDecision, OrderLog, Order, AccountInfo,
)
from core.entities.broker_capabilities import BrokerCapabilities

# Positions & deals
from core.entities.position_types import Position, OptionPosition, KnockOutPosition, FuturePosition
from core.entities.deal import Deal, DealState, OrderLeg, TpLevel

# Quotes
from core.entities.market_quotes import (
    Quote, OptionQuote, KnockOutQuote, CommodityQuote, ForexQuote, CryptoQuote,
)

# Market data
from core.entities.ohlc import OHLCData
from core.entities.market_data import MarketSession, MarketStatus, StockNews, MarketNews, EarningsTime

# Company & fundamentals
from core.entities.company_profile import CompanyProfile
from core.entities.earnings import EarningsCalendar, EarningsReport, EarningsCallTranscript

# Analyst
from core.entities.analyst_data import (
    GradeType, Grade, GradesSummary,
    AnalystRatingSnapshot, PriceTargetConsensus, PriceTargetNews,
)

__all__ = [
    # Base
    "BaseEntity", "parse_datetime", "parse_date", "to_float", "to_int",
    # Classification
    "InstrumentType", "TimeFrame", "CalendarEventType",
    # Broker
    "DataSource", "OrderSide", "TradeSide", "OrderType", "Action",
    "OrderStatus", "TradeDecision", "OrderLog", "Order", "AccountInfo",
    "BrokerCapabilities",
    # Positions & deals
    "Position", "OptionPosition", "KnockOutPosition", "FuturePosition",
    "Deal", "DealState", "OrderLeg", "TpLevel",
    # Quotes
    "Quote", "OptionQuote", "KnockOutQuote", "CommodityQuote", "ForexQuote", "CryptoQuote",
    # Market data
    "MarketSession", "OHLCData", "MarketStatus", "StockNews", "MarketNews", "EarningsTime",
    # Company
    "CompanyProfile",
    # Earnings
    "EarningsCalendar", "EarningsReport", "EarningsCallTranscript",
    # Analyst
    "GradeType", "Grade", "GradesSummary",
    "AnalystRatingSnapshot", "PriceTargetConsensus", "PriceTargetNews",
]
