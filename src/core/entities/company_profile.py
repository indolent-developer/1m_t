"""
core.entities.company_profile

CompanyProfile as a proper dataclass with a clean from_dict factory.
Identical field set to old class; drops manual __init__ boilerplate.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional


@dataclass
class CompanyProfile:
    # Identification
    symbol:             Optional[str]   = None
    company_name:       Optional[str]   = None
    cik:                Optional[str]   = None
    isin:               Optional[str]   = None
    cusip:              Optional[str]   = None

    # Market data
    price:              Optional[float] = None
    market_cap:         Optional[int]   = None
    beta:               Optional[float] = None
    last_dividend:      Optional[float] = None
    range:              Optional[str]   = None
    change:             Optional[float] = None
    change_percentage:  Optional[float] = None
    volume:             Optional[int]   = None
    average_volume:     Optional[int]   = None

    # Exchange
    exchange:           Optional[str]   = None
    exchange_full_name: Optional[str]   = None
    currency:           Optional[str]   = None

    # Fundamentals
    industry:           Optional[str]   = None
    sector:             Optional[str]   = None
    description:        Optional[str]   = None
    ceo:                Optional[str]   = None
    website:            Optional[str]   = None
    country:            Optional[str]   = None
    full_time_employees: str            = "0"

    # Contact
    phone:   str = ""
    address: str = ""
    city:    str = ""
    state:   str = ""
    zip:     str = ""
    image:   str = ""

    # Metadata
    ipo_date:            Optional[dt.date] = None
    default_image:       bool = False
    is_etf:              bool = False
    is_actively_trading: bool = False
    is_adr:              bool = False
    is_fund:             bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "CompanyProfile":
        ipo_raw = data.get("ipoDate")
        ipo_date = (
            dt.datetime.strptime(ipo_raw, "%Y-%m-%d").date()
            if ipo_raw else None
        )
        return cls(
            symbol=data.get("symbol"),
            company_name=data.get("companyName"),
            cik=data.get("cik"),
            isin=data.get("isin"),
            cusip=data.get("cusip"),
            price=data.get("price"),
            market_cap=data.get("marketCap"),
            beta=data.get("beta"),
            last_dividend=data.get("lastDividend"),
            range=data.get("range"),
            change=data.get("change"),
            change_percentage=data.get("changePercentage"),
            volume=data.get("volume"),
            average_volume=data.get("averageVolume"),
            exchange=data.get("exchange"),
            exchange_full_name=data.get("exchangeFullName"),
            currency=data.get("currency"),
            industry=data.get("industry"),
            sector=data.get("sector"),
            description=data.get("description"),
            ceo=data.get("ceo"),
            website=data.get("website"),
            country=data.get("country"),
            full_time_employees=data.get("fullTimeEmployees", "0"),
            phone=data.get("phone", ""),
            address=data.get("address", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            zip=data.get("zip", ""),
            image=data.get("image", ""),
            ipo_date=ipo_date,
            default_image=data.get("defaultImage", False),
            is_etf=data.get("isEtf", False),
            is_actively_trading=data.get("isActivelyTrading", False),
            is_adr=data.get("isAdr", False),
            is_fund=data.get("isFund", False),
        )
