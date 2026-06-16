from dataclasses import dataclass


@dataclass
class BrokerCapabilities:
    # Equities
    stock_trading: bool = False
    etf_trading: bool = False
    warrant_trading: bool = False

    # Derivatives
    options_trading: bool = False
    futures_trading: bool = False
    future_options_trading: bool = False
    knock_out_trading: bool = False
    cfd_trading: bool = False

    # Fixed Income
    bond_trading: bool = False

    # Funds
    mutual_fund_trading: bool = False

    # FX & Crypto
    forex_trading: bool = False
    crypto_trading: bool = False

    # Commodities
    commodity_trading: bool = False

    # Order features
    fractional_shares: bool = False
    trailing_stops: bool = False
    bracket_orders: bool = False        # entry + stop + target in one
    oco_orders: bool = False            # one-cancels-other
    extended_hours: bool = False
    short_selling: bool = False

    # Data features
    real_time_quotes: bool = False
    options_chain: bool = False
    historical_data: bool = False