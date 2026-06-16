"""
interfaces.console.cmd_search

/search command handler — queries all brokers concurrently.
"""
from __future__ import annotations

import asyncio


async def cmd_search(broker, args: list) -> None:
    if not args:
        print("Usage: /search SYMBOL")
        return
    sym = args[0].upper()

    from adapters.brokers.scalable_broker import search_symbol as _scalable
    from adapters.brokers.etoro_broker    import search_symbol as _etoro
    from adapters.brokers.capital_broker  import search_symbol as _capital
    from adapters.brokers.ibkr_broker     import search_symbol as _ibkr

    print(f"\n🔍  {sym}")
    print("  " + "─" * 82)
    print(f"  {'Broker':<10}  {'ID / ISIN':<24}  {'Name':<20}  {'Bid / Ask':<18}  Directions")
    print("  " + "─" * 82)

    results = await asyncio.gather(
        _scalable(broker, sym),
        _etoro(sym),
        _capital(sym),
        _ibkr(sym),
        return_exceptions=True,
    )
    for label, res in zip(["Scalable", "eToro", "Capital", "IBKR"], results):
        if isinstance(res, Exception):
            print(f"  {label:<10}  ✗  {str(res)[:60]}")
            continue
        found, ident, name, bid, ask, flags = res
        tick      = "✓" if found else "✗"
        price_str = f"{bid:.4f} / {ask:.4f}" if (bid or ask) else "—"
        flag_str  = "  ".join(flags) if flags else "—"
        print(f"  {label:<10}  {tick}  {str(ident):<24}  {str(name)[:20]:<20}  {price_str:<18}  {flag_str}")
    print()
