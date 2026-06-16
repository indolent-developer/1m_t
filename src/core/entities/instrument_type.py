"""
core.entities.instrument_type

Single source of truth for all tradeable instrument kinds.
Replaces the old AssetTypes enum and aligns with new-entities InstrumentType.
"""
from enum import Enum


class InstrumentType(Enum):
    # Equities
    STOCK        = "stock"
    ETF          = "etf"
    WARRANT      = "warrant"

    # Derivatives
    OPTION       = "option"
    FUTURE       = "future"
    FUTURE_OPTION = "future_option"   # IBKR-specific
    KNOCK_OUT    = "knock_out"        # Capital.com / Scalable
    CFD          = "cfd"              # Capital.com

    # Fixed Income
    BOND         = "bond"             # IBKR

    # Funds
    MUTUAL_FUND  = "mutual_fund"      # IBKR, Scalable

    # FX & Crypto
    FOREX        = "forex"
    CRYPTO       = "crypto"

    # Commodities
    COMMODITY    = "commodity"

    # Index (read-only / data only)
    INDEX        = "index"
